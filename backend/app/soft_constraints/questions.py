"""The five judgments asked of Jev, and nothing else.

One request, five independent questions over the same state. They are asked
together because they are independent -- none needs another's answer to be
meaningful -- and TypeSafe answers a batch in parallel, so five questions cost
one round trip rather than five.

**There is deliberately no Choice question here, and that absence is the
design** (this package's contract, and :mod:`app.soft_constraints.policy`).
A Choice would let the model pick ``accept`` / ``review`` / ``rebalance``
directly. That is the wrong shape for an application decision: the thresholds
at which a coordinator's attention is worth interrupting are a policy the
operator owns and will want to tune, they need to be auditable and identical
between two runs, and changing one must not require another inference call. So the model is
asked only what it can genuinely judge -- how balanced, how well-preferred,
how concerning, how good, how worth a second look -- and every one of those
comes back as a probability. Ordinary Python turns them into a status.

Question ids are for this package's code and are never sent to the model, so
each question's ``instructions`` carries its complete meaning rather than
relying on its name.

Score levels are 0-based: a five-level question returns an expected score
somewhere in ``0.0..4.0``, and :mod:`app.soft_constraints.evaluator`
normalizes that to ``0.0..1.0`` once, so no threshold anywhere else has to
know how many levels a question has.
"""

from __future__ import annotations

from typesafe_sdk import Noul, Score

__all__ = [
    "QUESTION_HUMAN_REVIEW",
    "QUESTION_OVERALL_QUALITY",
    "QUESTION_OVERUSE_CONCERN",
    "QUESTION_PREFERENCE_SATISFACTION",
    "QUESTION_WORKLOAD_FAIRNESS",
    "SCORE_QUESTIONS",
    "NOUL_QUESTIONS",
    "build_questions",
]

QUESTION_WORKLOAD_FAIRNESS = "workload_fairness"
QUESTION_PREFERENCE_SATISFACTION = "preference_satisfaction"
QUESTION_OVERUSE_CONCERN = "overuse_concern"
QUESTION_OVERALL_QUALITY = "soft_constraint_quality"
QUESTION_HUMAN_REVIEW = "human_review_warranted"

#: The Score questions, in the order a report reads best. Named here so the
#: evaluator and the tests agree on the set without either re-listing it.
SCORE_QUESTIONS = (
    QUESTION_WORKLOAD_FAIRNESS,
    QUESTION_PREFERENCE_SATISFACTION,
    QUESTION_OVERALL_QUALITY,
)

#: The Noul questions. Separate from the Scores because they are read
#: differently: a Noul is a probability of yes, with no confidence alongside
#: it, and 0.5 means "as likely as not", never "moderately".
NOUL_QUESTIONS = (
    QUESTION_OVERUSE_CONCERN,
    QUESTION_HUMAN_REVIEW,
)

#: Every Score question uses five levels, so one normalization rule covers
#: them all and a threshold reads the same against any of them.
SCORE_LEVELS = 5

_WORKLOAD_FAIRNESS = Score(
    instructions=(
        "How fairly is the serving work spread across the people listed in "
        "`people`? Fair means each person carries a share close to what the "
        "others carry, after allowing for how many events each was actually "
        "available for: somebody available for one event out of five is not "
        "being treated unfairly by receiving one assignment. Read "
        "`summary.assignment_spread`, `summary.mean_assignments_per_person` "
        "and `summary.unused_available_people` alongside the per-person rows. "
        "Judge the distribution of work only; ignore whether anyone's stated "
        "preferences were honoured."
    ),
    criteria=[
        "Grossly uneven: a small number of people carry nearly all the work "
        "while others who were available received nothing or almost nothing.",
        "Clearly uneven: the heaviest load is far above the lightest, and the "
        "gap is not explained by who was available.",
        "Mixed: roughly reasonable overall, but at least one person carries a "
        "noticeably heavier or lighter share than their availability explains.",
        "Broadly even: loads differ only slightly, and the differences track "
        "how available each person was.",
        "Even: every available person carries a share proportionate to their "
        "availability, with no one conspicuously over- or under-used.",
    ],
)

_PREFERENCE_SATISFACTION = Score(
    instructions=(
        "How well does this draft respect what the people listed in `people` "
        "asked for? Weigh `preferences_granted` against `preferences_declined` "
        "for each person, whether anyone is scheduled past the soft maximum in "
        "`preferred_max_assignments` (`over_preferred_limit`), and any `note`. "
        "A person who expressed no preference -- `preferred_max_assignments` "
        "null and no granted or declined counts -- is neither satisfied nor "
        "dissatisfied and should not drag the rating in either direction. "
        "Judge preference handling only; ignore whether the totals are evenly "
        "spread."
    ),
    criteria=[
        "Preferences are largely ignored: most expressed requests were "
        "refused, or people are scheduled well past the maximum they stated.",
        "Preferences are poorly served: refusals clearly outnumber grants, or "
        "at least one person is meaningfully past their stated maximum.",
        "Preferences are partly served: a mixed record of grants and refusals, "
        "or someone is slightly past their stated maximum.",
        "Preferences are mostly served: most requests were granted and nobody "
        "is past their stated maximum by more than a marginal amount.",
        "Preferences are fully served: expressed requests were granted and "
        "nobody is scheduled past the maximum they stated.",
    ],
)

_OVERALL_QUALITY = Score(
    instructions=(
        "Taking the whole draft together -- how the work is spread, whether "
        "stated preferences were respected, and whether anyone is being leaned "
        "on too heavily -- how good a piece of soft scheduling is this? This is "
        "the overall judgment, not a restatement of any single dimension: a "
        "draft can spread work evenly and still be poor if it does so by "
        "overriding what everybody asked for, and a draft with one small "
        "imbalance can still be good. Every hard scheduling rule is already "
        "satisfied, so nothing here is a rule violation; the question is "
        "whether a coordinator would be content to send this out."
    ),
    criteria=[
        "Poor: a coordinator would not send this out without redoing it.",
        "Weak: sendable only after specific changes; several people would "
        "reasonably object to their share or their treatment.",
        "Adequate: defensible but visibly imperfect; one or two people would "
        "reasonably raise a question about it.",
        "Good: a considerate draft with only minor imperfections that nobody "
        "is likely to object to.",
        "Excellent: work and preferences are handled about as well as the "
        "stated availability allows.",
    ],
)

_OVERUSE_CONCERN = Noul(
    instructions=(
        "Is at least one person in `people` being leaned on more heavily than "
        "is sustainable for a volunteer? Consider how their `assignments` "
        "compares with `event_count`, with what the other people carry, and "
        "with the soft maximum in `preferred_max_assignments` when they gave "
        "one. This asks specifically about a person carrying too much, not "
        "about the spread being uneven in general: a draft can be uneven "
        "without anybody's own load being unreasonable, and can overwork "
        "somebody even when everybody is loaded equally heavily."
    ),
)

_HUMAN_REVIEW = Noul(
    instructions=(
        "Should a human coordinator look over this draft before it is sent to "
        "the people on it? Answer yes when something about the way the work or "
        "the preferences have been handled would benefit from a person's "
        "judgment -- an allocation somebody would reasonably object to, a "
        "situation the recorded numbers do not fully capture, or context in a "
        "`note` that a scheduling rule cannot weigh. Answer no when the draft "
        "is unremarkable and sending it as it stands would be uncontroversial. "
        "Every hard scheduling rule is already satisfied, so this is never "
        "about catching a rule violation."
    ),
)


def build_questions() -> dict[str, Noul | Score]:
    """The five questions, as one mapping for a single ``system_one`` call.

    A fresh dict each call: the SDK's question objects are immutable pydantic
    models, but handing every caller the same mutable mapping invites one of
    them to add a sixth question and surprise the next.
    """
    return {
        QUESTION_WORKLOAD_FAIRNESS: _WORKLOAD_FAIRNESS,
        QUESTION_PREFERENCE_SATISFACTION: _PREFERENCE_SATISFACTION,
        QUESTION_OVERALL_QUALITY: _OVERALL_QUALITY,
        QUESTION_OVERUSE_CONCERN: _OVERUSE_CONCERN,
        QUESTION_HUMAN_REVIEW: _HUMAN_REVIEW,
    }
