from django.http import Http404, HttpResponseRedirect, HttpResponse, HttpResponseBadRequest
from django.shortcuts import render_to_response, get_object_or_404, redirect
from django.template import RequestContext, loader
from django.template.loader import render_to_string
from django.core.urlresolvers import reverse
from django.core.exceptions import PermissionDenied
from django.contrib.auth.decorators import user_passes_test, login_required
from django.contrib import messages
from django.db.models import Sum, Count
from django.conf import settings
from django.views.decorators.cache import cache_page
from ipware.ip import get_real_ip

from debate.models import Tournament, Round, Debate, Team, Venue, Adjudicator
from debate.models import AdjudicatorConflict, AdjudicatorInstitutionConflict, DebateAdjudicator, Speaker
from debate.models import Person, Checkin, Motion, ActionLog, BallotSubmission
from debate.models import AdjudicatorFeedback, ActiveVenue, ActiveTeam, ActiveAdjudicator
from debate import forms

from django.forms.models import modelformset_factory
from django.forms import Textarea

import datetime
from functools import wraps
import json

def get_ip_address(request):
    ip = get_real_ip(request)
    if ip is None:
        return "0.0.0.0"
    return ip

def redirect_round(to, round, **kwargs):
    return redirect(to, tournament_slug=round.tournament.slug,
                    round_seq=round.seq, *kwargs)

def redirect_tournament(to, tournament, **kwargs):
    return redirect(to, tournament_slug=tournament.slug, **kwargs)

def tournament_view(view_fn):
    @wraps(view_fn)
    def foo(request, tournament_slug, *args, **kwargs):
        return view_fn(request, request.tournament, *args, **kwargs)
    return foo

def round_view(view_fn):
    @wraps(view_fn)
    @tournament_view
    def foo(request, tournament, round_seq, *args, **kwargs):
        return view_fn(request, request.round, *args, **kwargs)
    return foo

def public_optional_tournament_view(config_option):
    def bar(view_fn):
        @wraps(view_fn)
        @tournament_view
        def foo(request, tournament, *args      , **kwargs):
            if tournament.config.get(config_option):
                return view_fn(request, tournament, *args, **kwargs)
            else:
                return redirect_tournament('public_index', tournament)
        return foo
    return bar

def public_optional_round_view(config_option):
    def bar(view_fn):
        @wraps(view_fn)
        @round_view
        def foo(request, round, *args, **kwargs):
            if round.tournament.config.get(config_option):
                return view_fn(request, round, *args, **kwargs)
            else:
                return redirect_tournament('public_index', round.tournament)
        return foo
    return bar

def admin_required(view_fn):
    return user_passes_test(lambda u: u.is_superuser)(view_fn)


def expect_post(view_fn):
    @wraps(view_fn)
    def foo(request, *args, **kwargs):
        if request.method != "POST":
            return HttpResponseBadRequest("Expected POST")
        return view_fn(request, *args, **kwargs)
    return foo


def r2r(request, template, extra_context=None):
    rc = RequestContext(request)
    if extra_context:
        rc.update(extra_context)
    return render_to_response(template, context_instance=rc)


def index(request):
    tournaments = Tournament.objects.all()

    if request.user.is_authenticated():
        if len(tournaments) == 1:
            return redirect('tournament_home', tournament_slug=tournaments[0].slug)
        else:
            return r2r(request, 'index.html', dict(tournaments=Tournament.objects.all()))
    else:
        if len(tournaments) == 1:
            return redirect('public_index', tournament_slug=tournaments[0].slug)
        else:
            return r2r(request, 'index.html', dict(tournaments=Tournament.objects.all()))

## Public UI

PUBLIC_PAGE_CACHE_TIMEOUT = 60

@cache_page(PUBLIC_PAGE_CACHE_TIMEOUT)
@tournament_view
def public_index(request, t):
    return r2r(request, 'public/index.html')


@cache_page(PUBLIC_PAGE_CACHE_TIMEOUT)
@public_optional_tournament_view('public_participants')
def public_participants(request, t):
    adjs = Adjudicator.objects.all()
    speakers = Speaker.objects.all()
    return r2r(request, "public/participants.html", dict(adjs=adjs, speakers=speakers))

@cache_page(PUBLIC_PAGE_CACHE_TIMEOUT)
@public_optional_tournament_view('public_draw')
def public_draw(request, t):
    r = t.current_round
    if r.draw_status == r.STATUS_RELEASED:
        draw = r.get_draw()
        return r2r(request, "public/draw_released.html", dict(draw=draw, round=r))
    else:
        return r2r(request, 'public/draw_unreleased.html', dict(draw=None, round=r))

@cache_page(PUBLIC_PAGE_CACHE_TIMEOUT)
@public_optional_tournament_view('public_team_standings')
def public_team_standings(request, t):
    round = t.current_round.prev

    # Find the most recent non-silent round
    while round is not None and round.silent:
        round = round.prev

    if round is not None and round.silent is False:

        from debate.models import TeamScore

        # Ranking by institution__name and reference isn't the same as ordering by
        # short_name, which is what we really want. But we can't rank by short_name,
        # because it's not a field (it's a property). So we'll do this in JavaScript.
        # The real purpose of this ordering is to obscure the *true* ranking of teams
        # - teams are not supposed to know rankings between teams on the same number
        # of wins.
        teams = Team.objects.order_by('institution__code', 'reference')

        rounds = Round.objects.filter(tournament=round.tournament,
                                    seq__lte=round.seq, silent=False).order_by('seq')

        def get_score(team, r):
            try:
                ts = TeamScore.objects.get(
                    ballot_submission__confirmed=True,
                    debate_team__team=team,
                    debate_team__debate__round=r,
                )
                debate = ts.debate_team.debate

                opposition = None
                if debate.neg_team == team:
                    opposition = ts.debate_team.debate.aff_team
                else:
                    opposition = ts.debate_team.debate.neg_team

                return ts.points, opposition
            except TeamScore.DoesNotExist:
                return None

        for team in teams:
            team.scores = [get_score(team, r) for r in rounds]
            # Do this manually, in case there are silent rounds
            team.wins = sum([score and score[0] or 0 for score in team.scores])

        return r2r(request, 'public/team_standings.html', dict(teams=teams, rounds=rounds, round=round))
    else:
        return r2r(request, 'public/index.html')


@cache_page(PUBLIC_PAGE_CACHE_TIMEOUT)
@public_optional_tournament_view('public_ballots')
def public_ballot_submit(request, t):
    r = t.current_round

    das = DebateAdjudicator.objects.filter(debate__round=r).select_related('adjudicator', 'debate')

    if r.draw_status == r.STATUS_RELEASED and r.motions_good_for_public:
        draw = r.get_draw()
        return r2r(request, 'public/add_ballot.html', dict(das=das))
    else:
        return r2r(request, 'public/add_ballot_unreleased.html', dict(das=None, round=r))

@cache_page(PUBLIC_PAGE_CACHE_TIMEOUT)
@public_optional_tournament_view('public_feedback')
def public_feedback_submit(request, t):
    adjudicators = Adjudicator.objects.all()
    teams = Team.objects.all()
    return r2r(request, 'public/add_feedback.html', dict(adjudicators=adjudicators, teams=teams))


@cache_page(3) # short cache - needs to update often
@public_optional_tournament_view('feedback_progress')
def public_feedback_progress(request, t):
    def calculate_coverage(submitted, total):
        if total == 0:
            return False # Don't show these ones
        elif submitted == 0:
            return 0
        else:
            return int((float(submitted) / float(total)) * 100)

    feedback = AdjudicatorFeedback.objects.all()
    adjudicators = Adjudicator.objects.all()
    teams = Team.objects.all()
    current_round = request.tournament.current_round.seq

    for adj in adjudicators:
        adj.total_ballots = 0
        adj.submitted_feedbacks = feedback.filter(source_adjudicator__adjudicator = adj)
        adjudications = DebateAdjudicator.objects.filter(adjudicator = adj)

        for item in adjudications:
            # Finding out the composition of their panel, tallying owed ballots
            if item.type == item.TYPE_CHAIR:
                adj.total_ballots += len(item.debate.adjudicators.trainees)
                adj.total_ballots += len(item.debate.adjudicators.panel)

            if item.type == item.TYPE_PANEL:
                # Panelists owe on chairs
                adj.total_ballots += 1

            if item.type == item.TYPE_TRAINEE:
                # Trainees owe on chairs
                adj.total_ballots += 1

        adj.submitted_ballots = max(adj.submitted_feedbacks.count(), 0)
        adj.owed_ballots = max((adj.total_ballots - adj.submitted_ballots), 0)
        adj.coverage = min(calculate_coverage(adj.submitted_ballots, adj.total_ballots), 100)

    for team in teams:
        team.submitted_ballots = max(feedback.filter(source_team__team = team).count(), 0)
        team.owed_ballots = max((current_round - team.submitted_ballots), 0)
        team.coverage = min(calculate_coverage(team.submitted_ballots, current_round), 100)

    return r2r(request, 'public/feedback_tab.html', dict(teams=teams, adjudicators=adjudicators))

@cache_page(PUBLIC_PAGE_CACHE_TIMEOUT)
@public_optional_tournament_view('public_motions')
def public_motions(request, t):
    rounds = Round.objects.filter(motions_released=True).order_by('-seq')
    return r2r(request, 'public/motions.html', dict(rounds=rounds))

## Tab

@cache_page(PUBLIC_PAGE_CACHE_TIMEOUT)
@public_optional_tournament_view('tab_released')
def public_team_tab(request, t):
    round = t.current_round
    from debate.models import TeamScore
    teams = Team.objects.ranked_standings(round)

    rounds = Round.objects.filter(tournament=round.tournament,
                                    seq__lte=round.seq).order_by('seq')

    def get_score(team, r):
        try:
            ts = TeamScore.objects.get(
                ballot_submission__confirmed=True,
                debate_team__team=team,
                debate_team__debate__round=r,
            )
            debate = ts.debate_team.debate
            opposition = None
            if debate.neg_team == team:
                opposition = ts.debate_team.debate.aff_team
            else:
                opposition = ts.debate_team.debate.neg_team

            return ts.score, ts.points, opposition
        except TeamScore.DoesNotExist:
            return None

    for team in teams:
        setattr(team, 'results_in', get_score(team, round) is not None)
        team.scores = [get_score(team, r) for r in rounds]

    return r2r(request, 'public/team_tab.html', dict(teams=teams,
            rounds=rounds, round=round))


@cache_page(PUBLIC_PAGE_CACHE_TIMEOUT)
@public_optional_tournament_view('tab_released')
def public_speaker_tab(request, t):
    round = t.current_round
    rounds = Round.objects.filter(tournament=round.tournament,
                                    seq__lte=round.seq).order_by('seq')
    speakers = Speaker.objects.standings(round)

    # TODO is there a way to do this without so many database hits?
    # Maybe using a select subquery?
    from debate.models import SpeakerScore
    def get_score(speaker, r):
        try:
            return SpeakerScore.objects.get(
                ballot_submission__confirmed=True,
                speaker=speaker,
                debate_team__debate__round=r,
                position__lte=3).score
        except SpeakerScore.DoesNotExist:
            return None

        # This was an issue once, not sure how, but if it ever happens,
        # fail gracefully.
        except SpeakerScore.MultipleObjectsReturned:
            print("Multiple speaker scores seen for speaker {0:s} in round {1:d}:".format(
                speaker.name, r.seq))
            for score in SpeakerScore.objects.filter(
                ballot_submission__confirmed=True,
                speaker=speaker,
                debate_team__debate__round=r,
                position__lte=3):
                print("   {dt:s}\n        position {pos:d}, ballot submission ID {id:d} (version {v:d}): score {score}".format(
                    dt=score.debate_team, pos=score.position, id=score.ballot_submission.id,
                    v=score.ballot_submission.version, score=score.score))
            return None

    for speaker in speakers:
        speaker.scores = [get_score(speaker, r) for r in rounds]
        speaker.results_in = get_score(speaker, round) is not None

    return r2r(request, 'public/speaker_tab.html', dict(speakers=speakers,
            rounds=rounds, round=round))

@cache_page(PUBLIC_PAGE_CACHE_TIMEOUT)
@public_optional_tournament_view('tab_released')
def public_replies_tab(request, t):
    round = t.current_round
    rounds = Round.objects.filter(tournament=round.tournament,
                                    seq__lte=round.seq).order_by('seq')
    speakers = Speaker.objects.reply_standings(round)

    from debate.models import SpeakerScore
    def get_score(speaker, r):
        try:
            return SpeakerScore.objects.get(
                ballot_submission__confirmed=True,
                speaker=speaker,
                debate_team__debate__round=r,
                position=4).score
        except SpeakerScore.DoesNotExist:
            return None

    for speaker in speakers:
        speaker.scores = [get_score(speaker, r) for r in rounds]
        try:
            # TODO detect if the speaker's *team's* ballot has been entered
            # for this round, and set results_in accordingly.
            #SpeakerScore.objects.get(speaker=speaker,
                                        #debate_team__debate__round=r,
                                        #position=4)
            speaker.results_in = True
        except SpeakerScore.DoesNotExist:
            speaker.results_in = False

    return r2r(request, 'public/reply_tab.html', dict(speakers=speakers,
            rounds=rounds, round=round))

@cache_page(PUBLIC_PAGE_CACHE_TIMEOUT)
@public_optional_tournament_view('motion_tab_released')
def public_motions_tab(request, t):
    round = t.current_round
    rounds = Round.objects.filter(tournament=round.tournament,
                                    seq__lte=round.seq).order_by('seq')
    motions = list()
    motions = Motion.objects.statistics(round=round)
    return r2r(request, 'public/motions_tab.html', dict(motions=motions))


@login_required
@tournament_view
def tournament_home(request, t):
    # Actions
    from debate.models import ActionLog
    a = ActionLog.objects.all().order_by('-id')[:25]

    # Speaker Scores
    from debate.models import SpeakerScore

    round = t.current_round

    # This should never happen, but if it does, fail semi-gracefully
    if round is None:
        if request.user.is_superuser:
            return HttpResponseBadRequest("You need to set the current round. <a href=\"/admin/debate/tournament\">Go to Django admin.</a>")
        else:
            raise Http404()

    rounds = Round.objects.filter(tournament=t, seq__lte=round.seq).order_by('seq')

    def get_round_stats(r):
        try:
            speaks = SpeakerScore.objects.filter(
            ballot_submission__confirmed=True,
            debate_team__debate__round=r,
            position__lte=3)

            round_min = min(speak.score for speak in speaks)
            round_avg = sum(speak.score for speak in speaks) / len(speaks)
            round_max = max(speak.score for speak in speaks)
            return round_min, round_avg, round_max
        except:
            # Lazy-catch all for possible errors
            return 0

    r_stats = [get_round_stats(r) for r in rounds]

    # Draw Status
    draw = round.get_draw()
    stats = {
        'none': draw.filter(result_status=Debate.STATUS_NONE).count(),
        'draft': draw.filter(result_status=Debate.STATUS_DRAFT).count(),
        'confirmed': draw.filter(result_status=Debate.STATUS_CONFIRMED).count(),
    }
    stats['in'] = stats['confirmed']
    stats['out'] = stats['none'] + stats['draft']
    if (stats['out'] + stats['in']) > 0:
        stats['pc'] = int(float(stats['in']) / (stats['out'] + stats['in']) * 100)
    else:
        stats['pc'] = 0

    if not request.user.is_superuser:
        return r2r(request, 'monkey/home.html', dict(stats=stats, round=round, actions=a, r_stats=r_stats))
    else:
        return r2r(request, 'tournament_home.html', dict(stats=stats, round=r, actions=a, r_stats=r_stats))

@admin_required
@tournament_view
def tournament_config(request, t):
    from debate.config import make_config_form

    context = {}
    if request.method == 'POST':
        form = make_config_form(t, request.POST)
        if form.is_valid():
            form.save()
            context['updated'] = True
            ActionLog.objects.log(type=ActionLog.ACTION_TYPE_CONFIG_EDIT, user=request.user)
    else:
        form = make_config_form(t)

    context['form'] = form

    return r2r(request, 'tournament_config.html', context)


@admin_required
@tournament_view
def feedback_progress(request, t):
    def calculate_coverage(submitted, total):
        if total == 0 or submitted == 0:
            return 0 # avoid divide-by-zero error
        else:
            return int((float(submitted) / float(total)) * 100)

    from debate.models import AdjudicatorFeedback
    feedback = AdjudicatorFeedback.objects.all()
    adjudicators = Adjudicator.objects.all()
    teams = Team.objects.all()

    # Teams only owe feedback on non silent rounds
    rounds_owed = request.tournament.rounds.filter(silent=False,
        draw_status=request.tournament.current_round.STATUS_RELEASED).count()

    for adj in adjudicators:
        adj.total_ballots = 0
        adj.submitted_feedbacks = feedback.filter(source_adjudicator__adjudicator = adj)
        adjudications = DebateAdjudicator.objects.filter(adjudicator = adj)

        for item in adjudications:
            # Finding out the composition of their panel, tallying owed ballots
            if item.type == item.TYPE_CHAIR:
                adj.total_ballots += len(item.debate.adjudicators.trainees)
                adj.total_ballots += len(item.debate.adjudicators.panel)

            if item.type == item.TYPE_PANEL:
                # Panelists owe on chairs
                adj.total_ballots += 1

            if item.type == item.TYPE_TRAINEE:
                # Trainees owe on chairs
                adj.total_ballots += 1

        adj.submitted_ballots = max(adj.submitted_feedbacks.count(), 0)
        adj.owed_ballots = max((adj.total_ballots - adj.submitted_ballots), 0)
        adj.coverage = min(calculate_coverage(adj.submitted_ballots, adj.total_ballots), 100)

    for team in teams:
        team.submitted_ballots = max(feedback.filter(source_team__team = team).count(), 0)
        team.owed_ballots = max((rounds_owed - team.submitted_ballots), 0)
        team.coverage = min(calculate_coverage(team.submitted_ballots, rounds_owed), 100)

    return r2r(request, 'wall_of_shame.html', dict(teams=teams, adjudicators=adjudicators))


@admin_required
@tournament_view
def draw_index(request, t):
    return r2r(request, 'draw_index.html')

@admin_required
@round_view
def round_index(request, round):
    return r2r(request, 'round_index.html')

@admin_required
@round_view
def confirm_increment(request, round):
    draw = round.get_draw()
    stats = {
        'none': draw.filter(result_status=Debate.STATUS_NONE, ballot_in=False).count(),
        'ballot_in': draw.filter(result_status=Debate.STATUS_NONE, ballot_in=True).count(),
        'draft': draw.filter(result_status=Debate.STATUS_DRAFT).count(),
        'confirmed': draw.filter(result_status=Debate.STATUS_CONFIRMED).count(),
    }
    return r2r(request, "round_increment.html", dict(stats=stats))

@admin_required
@expect_post
@round_view
def increment_round(request, round):

    return redirect_round('draw', round)

# public (for barcode checkins)
@round_view
def checkin(request, round):
    context = {}
    if request.method == 'POST':
        v = request.POST.get('barcode_id')
        try:
            barcode_id = int(v)
            p = Person.objects.get(barcode_id=barcode_id)
            ch, created = Checkin.objects.get_or_create(
                person = p,
                round = round
            )
            context['person'] = p

        except (ValueError, Person.DoesNotExist):
            context['unknown_id'] = v

    return r2r(request, 'checkin.html', context)

# public (for barcode checkins)
# public
@round_view
def post_checkin(request, round):
    v = request.POST.get('barcode_id')
    try:
        barcode_id = int(v)
        p = Person.objects.get(barcode_id=barcode_id)
        ch, created = Checkin.objects.get_or_create(
            person = p,
            round = round
        )

        message = p.checkin_message

        if not message:
            message = "Checked in %s" % p.name
        return HttpResponse(message)

    except (ValueError, Person.DoesNotExist):
        return HttpResponse("Unknown Id: %s" % v)

def _availability(request, round, model, context_name):

    items = getattr(round, '%s_availability' % model)()

    context = {
        context_name: items,
    }

    return r2r(request, '%s_availability.html' % model, context)


@admin_required
@round_view
def availability(request, round, model, context_name):
    return _availability(request, round, model, context_name)

@round_view
def checkin_results(request, round, model, context_name):
    return _availability(request, round, model, context_name)

def _update_availability(request, round, update_method, active_model, active_attr):

    if request.POST.get('copy'):
        prev_round = Round.objects.get(tournament=round.tournament,
                                       seq=round.seq-1)

        prev_objects = active_model.objects.filter(round=prev_round)
        available_ids = [getattr(o, '%s_id' % active_attr) for o in prev_objects]
        getattr(round, update_method)(available_ids)

        return HttpResponseRedirect(request.path.replace('update/', ''))

    available_ids = [int(a.replace("check_", "")) for a in request.POST.keys()
                     if a.startswith("check_")]

    getattr(round, update_method)(available_ids)

    ACTION_TYPES = {
        ActiveVenue:       ActionLog.ACTION_TYPE_AVAIL_VENUES_SAVE,
        ActiveTeam:        ActionLog.ACTION_TYPE_AVAIL_TEAMS_SAVE,
        ActiveAdjudicator: ActionLog.ACTION_TYPE_AVAIL_ADJUDICATORS_SAVE,
    }
    if active_model in ACTION_TYPES:
        ActionLog.objects.log(type=ACTION_TYPES[active_model], user=request.user, round=round)

    return HttpResponse("ok")

@admin_required
@expect_post
@round_view
def update_availability(request, round, update_method, active_model, active_attr):
    return _update_availability(request, round, update_method, active_model, active_attr)

@expect_post
@round_view
def checkin_update(request, round, update_method, active_model, active_attr):
    return _update_availability(request, round, update_method, active_model, active_attr)


@admin_required
@round_view
def draw_display_by_venue(request, round):
    draw = round.get_draw()
    return r2r(request, "draw_display_by_venue.html", dict(round=round, draw=draw))

@admin_required
@round_view
def draw_display_by_team(request, round):
    draw = round.get_draw()
    return r2r(request, "draw_display_by_team.html", dict(draw=draw))

@admin_required
@round_view
def draw(request, round):

    if round.draw_status == round.STATUS_NONE:
        return draw_none(request, round)

    if round.draw_status == round.STATUS_DRAFT:
        return draw_draft(request, round)

    if round.draw_status == round.STATUS_CONFIRMED:
        return draw_confirmed(request, round)

    if round.draw_status == round.STATUS_RELEASED:
        return draw_confirmed(request, round)

    raise


def draw_none(request, round):
    active_teams = round.active_teams.all()
    active_venues = round.active_venues.all()
    rooms = float(active_teams.count()) / 2
    return r2r(request, "draw_none.html", dict(active_teams=active_teams,
                                               active_venues=active_venues,
                                               rooms=rooms))


def draw_draft(request, round):
    draw = round.get_draw_with_standings(round)
    return r2r(request, "draw_draft.html", dict(draw=draw))


def draw_confirmed(request, round):
    draw = round.get_draw()
    rooms = float(round.active_teams.count()) / 2
    active_adjs = round.active_adjudicators.all()
    return r2r(request, "draw_confirmed.html", dict(draw=draw,
                                                    active_adjs=active_adjs,
                                                    rooms=rooms))

@admin_required
@round_view
def draw_with_standings(request, round):
    draw = round.get_draw_with_standings(round)
    return r2r(request, "draw_with_standings.html", dict(draw=draw))

@admin_required
@expect_post
@round_view
def create_draw(request, round):
    round.draw()
    ActionLog.objects.log(type=ActionLog.ACTION_TYPE_DRAW_CREATE,
        user=request.user, round=round)
    return redirect_round('draw', round)


@admin_required
@expect_post
@round_view
def confirm_draw(request, round):

    if round.draw_status != round.STATUS_DRAFT:
        return HttpResponseBadRequest("Draw status is not DRAFT")

    round.draw_status = round.STATUS_CONFIRMED
    round.save()
    ActionLog.objects.log(type=ActionLog.ACTION_TYPE_DRAW_CONFIRM,
        user=request.user, round=round)

    return redirect_round('draw', round)


@admin_required
@expect_post
@round_view
def release_draw(request, round):
    if round.draw_status != round.STATUS_CONFIRMED:
        return HttpResponseBadRequest("Draw status is not CONFIRMED")

    round.draw_status = round.STATUS_RELEASED
    round.save()
    ActionLog.objects.log(type=ActionLog.ACTION_TYPE_DRAW_RELEASE,
        user=request.user, round=round)

    return redirect_round('draw', round)


@admin_required
@expect_post
@round_view
def unrelease_draw(request, round):
    if round.draw_status != round.STATUS_RELEASED:
        return HttpResponseBadRequest("Draw status is not RELEASED")

    round.draw_status = round.STATUS_CONFIRMED
    round.save()
    ActionLog.objects.log(type=ActionLog.ACTION_TYPE_DRAW_UNRELEASE,
        user=request.user, round=round)

    return redirect_round('draw', round)



@admin_required
@expect_post
@round_view
def create_adj_allocation(request, round):

    if round.draw_status != round.STATUS_CONFIRMED:
        return HttpResponseBadRequest("Draw is not confirmed")

    from debate.adjudicator.hungarian import HungarianAllocator
    round.allocate_adjudicators(HungarianAllocator)

    return _json_adj_allocation(round.get_draw(), round.unused_adjudicators())


@admin_required
@expect_post
@round_view
def update_debate_importance(request, round):
    id = int(request.POST.get('debate_id'))
    im = int(request.POST.get('value'))
    debate = Debate.objects.get(pk=id)
    debate.importance = im
    debate.save()
    ActionLog.objects.log(type=ActionLog.ACTION_TYPE_DEBATE_IMPORTANCE_EDIT,
            user=request.user, debate=debate)
    return HttpResponse(im)

@admin_required
@round_view
def motions(request, round):
    motions = list()

    motions = Motion.objects.statistics(round=round)

    return r2r(request, "motions.html", dict(motions=motions))

@admin_required
@round_view
def motions_edit(request, round):
    MotionFormSet = modelformset_factory(Motion,
        widgets={'text': Textarea()},
        can_delete=True, extra=3, exclude=['round'])

    if request.method == 'POST':
        formset = MotionFormSet(request.POST, request.FILES)
        if formset.is_valid():
            for motion in formset.save(commit=False):
                motion.round = round
                motion.save()
                ActionLog.objects.log(type=ActionLog.ACTION_TYPE_MOTION_EDIT,
                    user=request.user, motion=motion)
            if 'submit' in request.POST:
                return redirect_round('motions', round)

    formset = MotionFormSet(queryset=Motion.objects.filter(round=round))

    return r2r(request, "motions_edit.html", dict(formset=formset))

@admin_required
@expect_post
@round_view
def release_motions(request, round):
    round.motions_released = True
    round.save()
    ActionLog.objects.log(type=ActionLog.ACTION_TYPE_MOTIONS_RELEASE,
        user=request.user, round=round)

    return redirect_round('motions', round)

@admin_required
@expect_post
@round_view
def unrelease_motions(request, round):
    round.motions_released = False
    round.save()
    ActionLog.objects.log(type=ActionLog.ACTION_TYPE_MOTIONS_UNRELEASE,
        user=request.user, round=round)

    return redirect_round('motions', round)

@admin_required
@expect_post
@round_view
def set_round_start_time(request, round):
    print request.POST
    time_text = request.POST["start_time"]
    try:
        time = datetime.datetime.strptime(time_text, "%H:%M").time()
    except ValueError, e:
        print e
        return redirect_round('draw', round)

    round.starts_at = time
    round.save()

    ActionLog.objects.log(type=ActionLog.ACTION_TYPE_ROUND_START_TIME_SET,
        user=request.user, round=round)

    return redirect_round('draw', round)

@login_required
@round_view
def results(request, round):
    if not request.user.is_superuser:
        return monkey_results(request, round)

    draw = round.get_draw()

    stats = {
        'none': draw.filter(result_status=Debate.STATUS_NONE, ballot_in=False).count(),
        'ballot_in': draw.filter(result_status=Debate.STATUS_NONE, ballot_in=True).count(),
        'draft': draw.filter(result_status=Debate.STATUS_DRAFT).count(),
        'confirmed': draw.filter(result_status=Debate.STATUS_CONFIRMED).count(),
    }

    show_motions_column = Motion.objects.filter(round=round).count() > 1

    return r2r(request, "results.html", dict(draw=draw, stats=stats, show_motions_column=show_motions_column))

def monkey_results(request, round):

    if round != request.tournament.current_round:
        raise Http404()

    draw = round.get_draw()
    draw = draw.filter(result_status__in=(Debate.STATUS_NONE, Debate.STATUS_DRAFT))
    return r2r(request, "monkey/results.html", dict(draw=draw))

@cache_page(PUBLIC_PAGE_CACHE_TIMEOUT)
@public_optional_round_view('public_results')
def public_results(request, round):
    # Can't see results for current round or later
    if round.seq >= round.tournament.current_round.seq or round.silent:
        raise Http404()
    draw = round.get_draw()
    show_motions_column = Motion.objects.filter(round=round).count() > 1
    show_splits = round.tournament.config.get('show_splitting_adjudicators')
    return r2r(request, "public/results_for_round.html", dict(
            draw=draw, show_motions_column=show_motions_column, show_splits=show_splits))

@cache_page(PUBLIC_PAGE_CACHE_TIMEOUT)
@public_optional_tournament_view('public_results')
def public_results_index(request, tournament):
    # Only rounds before current round
    rounds = Round.objects.filter(tournament=tournament,
            seq__lt=tournament.current_round.seq).order_by('seq')
    return r2r(request, "public/results_index.html", dict(rounds=rounds))

@login_required
@tournament_view
def edit_ballots(request, t, ballots_id):
    ballots = get_object_or_404(BallotSubmission, id=ballots_id)
    debate = ballots.debate

    if not request.user.is_superuser:
        template = 'monkey/enter_results.html'
        all_ballot_sets = debate.ballotsubmission_set.exclude(discarded=True).order_by('version')
        disable_confirm = request.user == ballots.user
    else:
        template = 'enter_results.html'
        all_ballot_sets = debate.ballotsubmission_set.order_by('version')
        disable_confirm = False

    identical_ballots_dict = debate.identical_ballots_dict
    for b in all_ballot_sets:
        if b in identical_ballots_dict:
            b.identical_ballot_versions = identical_ballots_dict[b]

    if request.method == 'POST':
        form = forms.BallotSetForm(ballots, request.POST)

        if form.is_valid():
            form.save()

            if ballots.discarded:
                action_type = ActionLog.ACTION_TYPE_BALLOT_DISCARD
            elif ballots.confirmed:
                action_type = ActionLog.ACTION_TYPE_BALLOT_CONFIRM
            else:
                action_type = ActionLog.ACTION_TYPE_BALLOT_EDIT
            ActionLog.objects.log(type=action_type, user=request.user,
                ballot_submission=ballots, ip_address=get_ip_address(request))

            return redirect_round('results', debate.round)
    else:
        form = forms.BallotSetForm(ballots)

    return r2r(request, template, dict(
        debate              =debate,
        form                =form,
        round               =debate.round,
        ballots             =ballots,
        all_ballot_sets     =all_ballot_sets,
        disable_confirm     =disable_confirm,
        new                 =False,
        ballot_not_singleton=all_ballot_sets.exclude(id=ballots_id).exists(),
        show_adj_contact    =True))

# Don't cache
@public_optional_tournament_view('public_ballots')
def public_new_ballots(request, t, adj_id):

    adjudicator = get_object_or_404(Adjudicator, id=adj_id)

    round = t.current_round
    if round.draw_status != Round.STATUS_RELEASED or not round.motions_released:
        return r2r(request, 'public/enter_results_error.html', dict(adjudicator=adjudicator, message='The draw and/or motions for the round haven\'t been released yet.'))
    try:
        da = DebateAdjudicator.objects.get(adjudicator=adjudicator, debate__round=round)
    except DebateAdjudicator.DoesNotExist:
        return r2r(request, 'public/enter_results_error.html', dict(adjudicator=adjudicator, message='It looks like you don\'t have a debate this round.'))

    debate = da.debate

    ip_address = get_ip_address(request)

    ballots = BallotSubmission(
        debate         = debate,
        submitter_type = BallotSubmission.SUBMITTER_PUBLIC,
        ip_address     = ip_address)

    existing_ballots = debate.ballotsubmission_set.exclude(discarded=True).count()

    if request.method == 'POST':
        form = forms.BallotSetForm(ballots, request.POST, password=True)

        if form.is_valid():
            form.save()

            ActionLog.objects.log(type=ActionLog.ACTION_TYPE_BALLOT_SUBMIT,
                    ballot_submission=ballots, ip_address=ip_address)
            return r2r(request, 'public/success.html', dict(success_kind="ballot"))

    else:
        form = forms.BallotSetForm(ballots, password=True)

    return r2r(request, 'public/enter_results.html', dict(
        debate          =debate,
        form            =form,
        round           =round,
        ballots         =ballots,
        adjudicator     =adjudicator,
        existing_ballots=existing_ballots,
        show_adj_contact=False))

@login_required
@tournament_view
def new_ballots(request, t, debate_id):
    debate = get_object_or_404(Debate, id=debate_id)
    ip_address = get_ip_address(request)

    ballots = BallotSubmission(
        debate        =debate,
        submitter_type=BallotSubmission.SUBMITTER_TABROOM,
        user          =request.user,
        ip_address    =ip_address)

    if not request.user.is_superuser:
        template = 'monkey/enter_results.html'
        all_ballot_sets = debate.ballotsubmission_set.exclude(discarded=True).order_by('version')
    else:
        template = 'enter_results.html'
        all_ballot_sets = debate.ballotsubmission_set.order_by('version')

    if request.method == 'POST':
        form = forms.BallotSetForm(ballots, request.POST)

        if form.is_valid():
            form.save()

            ActionLog.objects.log(type=ActionLog.ACTION_TYPE_BALLOT_CREATE, user=request.user,
                    ballot_submission=ballots, ip_address=ip_address)

            return redirect_round('results', debate.round)

    else:
        form = forms.BallotSetForm(ballots)

    return r2r(request, template, dict(
        debate              =debate,
        form                =form,
        round               =debate.round,
        ballots             =ballots,
        all_ballot_sets     =all_ballot_sets,
        new                 =True,
        ballot_not_singleton=all_ballot_sets.exists(),
        show_adj_contact    =True))

@admin_required
@round_view
def team_standings(request, round):
    from debate.models import TeamScore
    teams = Team.objects.ranked_standings(round)

    rounds = Round.objects.filter(tournament=round.tournament,
                                  seq__lte=round.seq).order_by('seq')

    def get_score(team, r):
        try:
            ts = TeamScore.objects.get(
                ballot_submission__confirmed=True,
                debate_team__team=team,
                debate_team__debate__round=r,
            )
            debate = ts.debate_team.debate
            opposition = None
            if debate.neg_team == team:
                opposition = ts.debate_team.debate.aff_team
            else:
                opposition = ts.debate_team.debate.neg_team

            return ts.score, ts.points, opposition
        except TeamScore.DoesNotExist:
            return None

    for team in teams:
        setattr(team, 'results_in', get_score(team, round) is not None)
        team.scores = [get_score(team, r) for r in rounds]

    return r2r(request, 'team_standings.html', dict(teams=teams, rounds=rounds))


@admin_required
@round_view
def speaker_standings(request, round):
    rounds = Round.objects.filter(tournament=round.tournament,
                                  seq__lte=round.seq).order_by('seq')
    speakers = Speaker.objects.standings(round)

    # TODO is there a way to do this without so many database hits?
    # Maybe using a select subquery?
    from debate.models import SpeakerScore
    def get_score(speaker, r):
        try:
            return SpeakerScore.objects.get(
                ballot_submission__confirmed=True,
                speaker=speaker,
                debate_team__debate__round=r,
                position__lte=3).score
        except SpeakerScore.DoesNotExist:
            return None

        # This was an issue once, not sure how, but if it ever happens,
        # fail gracefully.
        except SpeakerScore.MultipleObjectsReturned:
            print("Multiple speaker scores seen for speaker {0:s} in round {1:d}:".format(
                speaker.name, r.seq))
            for score in SpeakerScore.objects.filter(
                ballot_submission__confirmed=True,
                speaker=speaker,
                debate_team__debate__round=r,
                position__lte=3):
                print("   {dt:s}\n        position {pos:d}, ballot submission ID {id:d} (version {v:d}): score {score}".format(
                    dt=score.debate_team, pos=score.position, id=score.ballot_submission.id,
                    v=score.ballot_submission.version, score=score.score))
            return None

    for speaker in speakers:
        speaker.scores = [get_score(speaker, r) for r in rounds]
        speaker.results_in = get_score(speaker, round) is not None

    return r2r(request, 'speaker_standings.html', dict(speakers=speakers,
                                                       rounds=rounds))
    # Comment out above line and uncomment below line to prevent access to
    # speaker standings.
    #return r2r(request, 'speaker_standings.html', dict(speakers=None,
                                                       #rounds=rounds))

@admin_required
@round_view
def reply_standings(request, round):
    rounds = Round.objects.filter(tournament=round.tournament,
                                  seq__lte=round.seq).order_by('seq')
    speakers = Speaker.objects.reply_standings(round)

    from debate.models import SpeakerScore
    def get_score(speaker, r):
        try:
            return SpeakerScore.objects.get(
                ballot_submission__confirmed=True,
                speaker=speaker,
                debate_team__debate__round=r,
                position=4).score
        except SpeakerScore.DoesNotExist:
            return None

    for speaker in speakers:
        speaker.scores = [get_score(speaker, r) for r in rounds]
        try:
            # TODO detect if the speaker's *team's* ballot has been entered
            # for this round, and set results_in accordingly.
            #SpeakerScore.objects.get(speaker=speaker,
                                     #debate_team__debate__round=r,
                                     #position=4)
            speaker.results_in = True
        except SpeakerScore.DoesNotExist:
            speaker.results_in = False

    return r2r(request, 'reply_standings.html', dict(speakers=speakers,
                                                     rounds=rounds))

@admin_required
@round_view
def draw_venues_edit(request, round):

    draw = round.get_draw()
    return r2r(request, "draw_venues_edit.html", dict(draw=draw))


@admin_required
@expect_post
@round_view
def save_venues(request, round):

    def v_id(a):
        try:
            return int(request.POST[a].split('_')[1])
        except IndexError:
            return None
    data = [(int(a.split('_')[1]), v_id(a))
             for a in request.POST.keys()]

    debates = Debate.objects.in_bulk([d_id for d_id, _ in data])
    venues = Venue.objects.in_bulk([v_id for _, v_id in data])
    for debate_id, venue_id in data:
        if venue_id == None:
            debates[debate_id].venue = None
        else:
            debates[debate_id].venue = venues[venue_id]

        debates[debate_id].save()

    ActionLog.objects.log(type=ActionLog.ACTION_TYPE_VENUES_SAVE,
        user=request.user, round=round)

    return HttpResponse("ok")


@admin_required
@round_view
def draw_adjudicators_edit(request, round):
    draw = round.get_draw()
    adj0 = Adjudicator.objects.first()
    return r2r(request, "draw_adjudicators_edit.html", dict(draw=draw, adj0=adj0))

def _json_adj_allocation(debates, unused_adj):

    obj = {}

    def _adj(a):
        return {
            'id': a.id,
            'name': a.name + " (" + a.institution.short_code + ")",
            'is_trainee': a.is_trainee,
        }

    def _debate(d):
        r = {}
        if d.adjudicators.chair:
            r['chair'] = _adj(d.adjudicators.chair)
        r['panel'] = [_adj(a) for a in d.adjudicators.panel]
        r['trainees'] = [_adj(a) for a in d.adjudicators.trainees]
        return r

    obj['debates'] = dict((d.id, _debate(d)) for d in debates)
    obj['unused'] = [_adj(a) for a in unused_adj]

    return HttpResponse(json.dumps(obj))


@admin_required
@round_view
def draw_adjudicators_get(request, round):

    draw = round.get_draw()

    return _json_adj_allocation(draw, round.unused_adjudicators())


@admin_required
@round_view
def save_adjudicators(request, round):
    if request.method != "POST":
        return HttpResponseBadRequest("Expected POST")

    def id(s):
        s = s.replace('[]', '')
        return int(s.split('_')[1])

    debate_ids = set(id(a) for a in request.POST);
    debates = Debate.objects.in_bulk(list(debate_ids));
    debate_adjudicators = {}
    for d_id, debate in debates.items():
        a = debate.adjudicators
        a.delete()
        debate_adjudicators[d_id] = a

    for key, vals in request.POST.lists():
        if key.startswith("chair_"):
            debate_adjudicators[id(key)].chair = vals[0]
        if key.startswith("panel_"):
            for val in vals:
                debate_adjudicators[id(key)].panel.append(val)
        if key.startswith("trainees_"):
            for val in vals:
                debate_adjudicators[id(key)].trainees.append(val)

    # We don't do any validity checking here, so that the adjudication
    # core can save a work in progress.

    for d_id, alloc in debate_adjudicators.items():
        alloc.save()

    ActionLog.objects.log(type=ActionLog.ACTION_TYPE_ADJUDICATORS_SAVE,
        user=request.user, round=round)

    return HttpResponse("ok")


@admin_required
@round_view
def adj_conflicts(request, round):

    data = {
        'conflict': {},
        'history': {},
    }

    def add(type, adj_id, target_id):
        if adj_id not in data[type]:
            data[type][adj_id] = []
        data[type][adj_id].append(target_id)

    for ac in AdjudicatorConflict.objects.all():
        add('conflict', ac.adjudicator_id, ac.team_id)

    for ic in AdjudicatorInstitutionConflict.objects.all():
        for team in Team.objects.filter(institution=ic.institution):
            add('conflict', ic.adjudicator_id, team.id)

    history = DebateAdjudicator.objects.filter(
        debate__round__seq__lt = round.seq,
    )

    for da in history:
        add('history', da.adjudicator_id, da.debate.aff_team.id)
        add('history', da.adjudicator_id, da.debate.neg_team.id)

    return HttpResponse(json.dumps(data), content_type="text/json")


@admin_required
@tournament_view
def adj_scores(request, t):
    data = {}

    #TODO: make round-dependent
    for adj in Adjudicator.objects.all():
        data[adj.id] = adj.score

    return HttpResponse(json.dumps(data), content_type="text/json")


@login_required
@tournament_view
def adj_feedback(request, t):
    if not request.user.is_superuser:
        template = 'monkey/adjudicator_feedback.html'
    else:
        template = 'adjudicator_feedback.html'

    adjudicators = Adjudicator.objects.all()
    return r2r(request, template, dict(adjudicators=adjudicators))


@login_required
@tournament_view
def get_adj_feedback(request, t):

    adj = get_object_or_404(Adjudicator, pk=int(request.GET['id']))
    feedback = adj.get_feedback()
    data = [ [unicode(str(f.version) + (f.confirmed and "*" or "")),
              unicode(f.round),
              f.debate.bracket,
              unicode(f.debate),
              unicode(f.source),
              f.score,
              {None: "Unsure", True: "Yes", False: "No"}[f.agree_with_decision],
              f.comments,
             ] for f in feedback ]

    return HttpResponse(json.dumps({'aaData': data}), content_type="text/json")

# Don't cache
@public_optional_tournament_view('public_feedback')
def public_enter_feedback_adjudicator(request, t, adj_id):

    source = get_object_or_404(Adjudicator, id=adj_id)
    include_panellists = request.tournament.config.get('panellist_feedback_enabled') > 0
    ip_address = get_ip_address(request)
    source_name = source.name

    submission_fields = {
        'submitter_type': AdjudicatorFeedback.SUBMITTER_PUBLIC,
        'ip_address'    : ip_address
    }

    if request.method == "POST":
        form = forms.make_feedback_form_class_for_public_adj(source, submission_fields, include_panellists=include_panellists)(request.POST)
        if form.is_valid():
            adj_feedback = form.save()
            ActionLog.objects.log(type=ActionLog.ACTION_TYPE_FEEDBACK_SUBMIT,
                    ip_address=ip_address, adjudicator_feedback=adj_feedback)
            return r2r(request, 'public/success.html', dict(success_kind="feedback"))
    else:
        form = forms.make_feedback_form_class_for_public_adj(source, submission_fields, include_panellists=include_panellists)()

    return r2r(request, 'public/enter_feedback_adj.html', dict(source_name=source_name, form=form))

# Don't cache
@public_optional_tournament_view('public_feedback')
def public_enter_feedback_team(request, t, team_id):

    source = get_object_or_404(Team, id=team_id)
    ip_address = get_ip_address(request)
    source_name = source.short_name

    submission_fields = {
        'submitter_type': AdjudicatorFeedback.SUBMITTER_PUBLIC,
        'ip_address'    : ip_address
    }

    if request.method == "POST":
        form = forms.make_feedback_form_class_for_public_team(source, submission_fields)(request.POST)
        if form.is_valid():
            adj_feedback = form.save()
            ActionLog.objects.log(type=ActionLog.ACTION_TYPE_FEEDBACK_SUBMIT,
                    ip_address=ip_address, adjudicator_feedback=adj_feedback)
            return r2r(request, 'public/success.html', dict(success_kind="feedback"))
    else:
        form = forms.make_feedback_form_class_for_public_team(source, submission_fields)()

    return r2r(request, 'public/enter_feedback_team.html', dict(source_name=source_name, form=form))

@login_required
@tournament_view
def enter_feedback(request, t, adj_id):

    adj = get_object_or_404(Adjudicator, id=adj_id)
    ip_address = get_ip_address(request)

    if not request.user.is_superuser:
        template = 'monkey/enter_feedback.html'
    else:
        template = 'enter_feedback.html'

    submission_fields = {
        'submitter_type': AdjudicatorFeedback.SUBMITTER_TABROOM,
        'user'          : request.user,
        'ip_address'    : ip_address
    }

    if request.method == "POST":
        form = forms.make_feedback_form_class_for_tabroom(adj, submission_fields)(request.POST)
        if form.is_valid():
            adj_feedback = form.save()
            ActionLog.objects.log(type=ActionLog.ACTION_TYPE_FEEDBACK_SAVE,
                user=request.user, adjudicator_feedback=adj_feedback)
            return redirect_tournament('adj_feedback', t)
    else:
        form = forms.make_feedback_form_class_for_tabroom(adj, submission_fields)()

    return r2r(request, template, dict(adj=adj, form=form))

@admin_required
@round_view
def ballot_checkin(request, round):
    ballots_left = ballot_checkin_number_left(round)
    return r2r(request, 'ballot_checkin.html', dict(ballots_left=ballots_left))

class DebateBallotCheckinError(Exception):
    pass

def get_debate_from_ballot_checkin_request(request, round):
    # Called by the submit button on the ballot checkin form.
    # Returns the message that should go in the "success" field.
    v = request.POST.get('venue')

    try:
        venue = Venue.objects.get(name__iexact=v)
    except Venue.DoesNotExist:
        raise DebateBallotCheckinError('There aren\'t any venues with the name "' + v + '".')

    try:
        debate = Debate.objects.get(round=round, venue=venue)
    except Debate.DoesNotExist:
        raise DebateBallotCheckinError('There wasn\'t a debate in venue ' + venue.name + ' this round.')

    if debate.ballot_in:
        raise DebateBallotCheckinError('The ballot for venue ' + venue.name + ' has already been checked in.')

    return debate

def ballot_checkin_number_left(round):
    count = Debate.objects.filter(round=round, ballot_in=False).count()
    return count

@admin_required
@round_view
def ballot_checkin_get_details(request, round):
    try:
        debate = get_debate_from_ballot_checkin_request(request, round)
    except DebateBallotCheckinError, e:
        data = {'exists': False, 'message': str(e)}
        return HttpResponse(json.dumps(data))

    obj = dict()

    obj['exists'] = True
    obj['venue'] = debate.venue.name
    obj['aff_team'] = debate.aff_team.short_name
    obj['neg_team'] = debate.neg_team.short_name

    adjs = debate.adjudicators
    adj_names = [adj.name for type, adj in adjs if type != DebateAdjudicator.TYPE_TRAINEE]
    obj['num_adjs'] = len(adj_names)
    obj['adjudicators'] = adj_names

    obj['ballots_left'] = ballot_checkin_number_left(round)

    return HttpResponse(json.dumps(obj))

@admin_required
@round_view
def post_ballot_checkin(request, round):
    try:
        debate = get_debate_from_ballot_checkin_request(request, round)
    except DebateBallotCheckinError, e:
        data = {'exists': False, 'message': str(e)}
        return HttpResponse(json.dumps(data))

    debate.ballot_in = True
    debate.save()

    ActionLog.objects.log(type=ActionLog.ACTION_TYPE_BALLOT_CHECKIN,
            user=request.user, debate=debate)

    obj = dict()

    obj['success'] = True
    obj['venue'] = debate.venue.name
    obj['debate_description'] = debate.aff_team.short_name + " vs " + debate.neg_team.short_name

    obj['ballots_left'] = ballot_checkin_number_left(round)

    return HttpResponse(json.dumps(obj))
