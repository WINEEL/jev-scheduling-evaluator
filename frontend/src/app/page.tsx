"use client";

/**
 * The whole application: three invented drafts, one live judgment.
 *
 * It exists to show one distinction that is hard to explain in prose and
 * obvious in a layout:
 *
 *     hard constraints -> a deterministic engine (already satisfied)
 *     soft constraints -> Jev's probabilistic judgment
 *     final action     -> deterministic Python policy
 *
 * So the page is three panels in that order, and the middle one is visually
 * distinct from the two either side of it. Everything in the first and third
 * panels is arithmetic this repository can prove; everything in the middle is
 * a probability a model returned. A reader who takes nothing else from the
 * screen should come away knowing which is which.
 *
 * **Nothing typed here reaches the model.** The only thing sent is a scenario
 * name from a fixed list of three; the draft itself is built in
 * `app/soft_constraints/scenarios.py`. There is no field on this page that
 * accepts a person, and no request body that could carry one.
 *
 * The wording, tone and formatting rules are in `lib/presentation.ts`, tested
 * there. This file is layout and state.
 */

import { useCallback, useEffect, useState } from "react";

import {
  JevDemoError,
  evaluateScenario,
  getScenarios,
  isAbortError,
} from "@/lib/client";
import type {
  JevDemoEvaluation,
  JevDemoNoulAnswer,
  JevDemoScenario,
  JevDemoScoreAnswer,
} from "@/lib/types";
import {
  confidenceBand,
  isDistributionSpread,
  noulReading,
  percent,
  questionLabel,
  reasonLabel,
  reasonThreshold,
  scoreOutOf,
  statusExplanation,
  statusLabel,
  statusTone,
} from "@/lib/presentation";

export default function HomePage() {
  const [scenarios, setScenarios] = useState<JevDemoScenario[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [isLoading, setIsLoading] = useState(true);

  const [evaluation, setEvaluation] = useState<JevDemoEvaluation | null>(null);
  const [evaluateError, setEvaluateError] = useState<unknown>(null);
  const [isEvaluating, setIsEvaluating] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    getScenarios(controller.signal)
      .then((result) => {
        setScenarios(result.scenarios);
        setSelected(result.scenarios[0]?.name ?? null);
        setLoadError(null);
        setIsLoading(false);
      })
      .catch((cause: unknown) => {
        // An abort is not an outcome; leaving the loading flag alone keeps the
        // loading state rather than announcing a result nobody got.
        if (isAbortError(cause)) return;
        setLoadError(cause);
        setIsLoading(false);
      });
    return () => controller.abort();
  }, []);

  const current = scenarios?.find((entry) => entry.name === selected) ?? null;

  const choose = useCallback((name: string) => {
    setSelected(name);
    // The old judgment described a different draft. Keeping it on screen while
    // a new scenario is shown beside it is the one way this page could
    // genuinely mislead somebody.
    setEvaluation(null);
    setEvaluateError(null);
  }, []);

  const evaluate = useCallback(() => {
    if (selected === null) return;
    setIsEvaluating(true);
    setEvaluateError(null);
    evaluateScenario(selected)
      .then((result) => {
        setEvaluation(result);
        setIsEvaluating(false);
      })
      .catch((cause: unknown) => {
        setEvaluateError(cause);
        setIsEvaluating(false);
      });
  }, [selected]);

  return (
    <>
      <SyntheticDataBanner />

      <h1 className="page__title">Soft-constraint evaluation with Jev</h1>
      <p className="page__subtitle">
        Three invented scheduling drafts, judged by a System One model, with the
        final decision made by ordinary Python.
      </p>

      <LayerLegend />

      {isLoading && (
        <p className="muted section" role="status">
          Loading the demo scenarios&hellip;
        </p>
      )}
      {loadError !== null && (
        <div className="section">
          <DemoError error={loadError} title="The demo scenarios could not be loaded" />
        </div>
      )}

      {scenarios !== null && current !== null && (
        <>
          <ScenarioPicker
            scenarios={scenarios}
            selected={current.name}
            onChoose={choose}
            disabled={isEvaluating}
          />

          <DeterministicPanel scenario={current} />

          <section className="section" aria-label="Run the evaluation">
            <div className="jev-run">
              <button
                type="button"
                className="button button--primary"
                onClick={evaluate}
                disabled={isEvaluating}
              >
                {isEvaluating ? "Asking Jev…" : "Evaluate with Jev"}
              </button>
              <p className="muted small jev-run__hint">
                One call to TypeSafe, five questions, about a second. The browser sends
                only the scenario ID; the server builds the fixed synthetic state and
                evaluates it with TypeSafe.
              </p>
            </div>

            {evaluateError !== null && (
              <div className="jev-run__error">
                <DemoError error={evaluateError} title="Jev could not be asked" />
              </div>
            )}
          </section>

          {evaluation !== null && <ResultPanel evaluation={evaluation} />}
        </>
      )}
    </>
  );
}

/**
 * Said first, and unmissable — but not loud.
 *
 * Every screenshot of this page will carry it, which is the point: a picture
 * of a scheduling judgment that does not say "invented" is a picture somebody
 * can mistake for a real roster. It earns its place by being readable rather
 * than by being large, so it sits above the title as one quiet line.
 */
function SyntheticDataBanner() {
  return (
    <div className="jev-synthetic" role="note">
      <span className="jev-synthetic__label">Synthetic demo data</span>
      <p className="jev-synthetic__text">
        Every volunteer on this page is invented — <em>Volunteer A</em> through{" "}
        <em>Volunteer F</em>, built on the server from a fixed list of three
        scenarios. No real person, team, date or stored record is involved
        anywhere in this project.
      </p>
    </div>
  );
}

/** The distinction the whole page exists to make, stated once at the top. */
function LayerLegend() {
  return (
    <section className="section jev-legend" aria-label="How the three layers divide">
      <div className="jev-layer jev-layer--deterministic">
        <p className="jev-layer__kind">Hard constraints</p>
        <p className="jev-layer__owner">Deterministic engine</p>
        <p className="small jev-layer__body">
          Qualification, availability, per-person limits, exclusions, gaps. A
          solver decides these exactly, and they are already satisfied in every
          draft below.
        </p>
      </div>
      <span className="jev-legend__arrow" aria-hidden="true">
        →
      </span>
      <div className="jev-layer jev-layer--model">
        <p className="jev-layer__kind">Soft constraints</p>
        <p className="jev-layer__owner">Jev — probabilistic</p>
        <p className="small jev-layer__body">
          Fairness, preferences, and whether somebody is being leaned on involve
          softer tradeoffs, so Jev provides probabilistic judgments.
        </p>
      </div>
      <span className="jev-legend__arrow" aria-hidden="true">
        →
      </span>
      <div className="jev-layer jev-layer--deterministic">
        <p className="jev-layer__kind">Final action</p>
        <p className="jev-layer__owner">Deterministic policy</p>
        <p className="small jev-layer__body">
          Ordinary Python turns those probabilities into one status. The
          thresholds are ours, the same judgment always gives the same status,
          and no model is asked.
        </p>
      </div>
    </section>
  );
}

function ScenarioPicker({
  scenarios,
  selected,
  onChoose,
  disabled,
}: {
  scenarios: JevDemoScenario[];
  selected: string;
  onChoose: (name: string) => void;
  disabled: boolean;
}) {
  return (
    <section className="section">
      <h2 className="section__title">Choose a synthetic draft</h2>
      <div className="jev-scenarios" role="radiogroup" aria-label="Synthetic draft">
        {scenarios.map((scenario) => (
          <button
            key={scenario.name}
            type="button"
            role="radio"
            aria-checked={scenario.name === selected}
            className={`jev-scenario${scenario.name === selected ? " jev-scenario--selected" : ""}`}
            onClick={() => onChoose(scenario.name)}
            disabled={disabled}
          >
            {/* Shape as well as colour: the ring fills in, so the choice reads
                in greyscale and to a colour-blind reader. */}
            <span className="jev-scenario__dot" aria-hidden="true" />
            <span className="jev-scenario__name">{scenario.name}</span>
            <span className="small jev-scenario__summary">{scenario.summary_line}</span>
          </button>
        ))}
      </div>
    </section>
  );
}

/** Panel one: what deterministic code already knows. No model involved. */
function DeterministicPanel({ scenario }: { scenario: JevDemoScenario }) {
  const summary = scenario.workload_summary;
  return (
    <section className="section">
      <h2 className="section__title">
        What the deterministic layer already knows{" "}
        <span className="badge">Exact</span>
      </h2>
      <div className="card">
        <p className="jev-facts">{scenario.scenario}</p>
        <p className="card__meta">
          {scenario.event_count} events · {summary.people_count} volunteers ·
          every hard constraint already satisfied
        </p>

        <ul className="stats jev-stats">
          <Stat label="Assignment spread" value={String(summary.assignment_spread)} alert={summary.assignment_spread > 1} />
          <Stat label="Mean per person" value={summary.mean_assignments_per_person.toFixed(2)} />
          <Stat label="Available but unused" value={String(summary.unused_available_people)} alert={summary.unused_available_people > 0} />
          <Stat label="Past their stated max" value={String(summary.people_over_preferred_limit)} alert={summary.people_over_preferred_limit > 0} />
          <Stat
            label="Preferences granted"
            value={summary.preference_grant_rate === null ? "none stated" : percent(summary.preference_grant_rate)}
          />
        </ul>

        <div className="table-scroll jev-people">
          <table>
            <caption>Every row is invented. References, not names.</caption>
            <thead>
              <tr>
                <th scope="col">Volunteer</th>
                <th scope="col">Available</th>
                <th scope="col">Assigned</th>
                <th scope="col">Asked for at most</th>
                <th scope="col">Preferences</th>
                <th scope="col">Note</th>
              </tr>
            </thead>
            <tbody>
              {scenario.people.map((person) => (
                <tr key={person.reference} className={person.over_preferred_limit ? "jev-row--over" : undefined}>
                  <th scope="row">{person.reference}</th>
                  <td>{person.available_events}</td>
                  <td>{person.assignments}</td>
                  <td>
                    {person.preferred_max_assignments === null
                      ? <span className="muted">none stated</span>
                      : person.preferred_max_assignments}
                    {person.over_preferred_limit && (
                      <> <span className="badge badge--warning">over</span></>
                    )}
                  </td>
                  <td>
                    {person.preferences_granted + person.preferences_declined === 0 ? (
                      <span className="muted">—</span>
                    ) : (
                      `${person.preferences_granted} granted, ${person.preferences_declined} declined`
                    )}
                  </td>
                  <td className="small muted">{person.note ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {scenario.notes.map((note) => (
          <p key={note} className="small jev-note">{note}</p>
        ))}
      </div>
    </section>
  );
}

/**
 * The answer, in its two halves.
 *
 * The split is the whole point, so it is a seam a screenshot can show: the top
 * half is the model's, in the model's colour, and holds nothing but
 * probabilities; the bottom half is Python's, in the page's own colours, and
 * holds the one status the application acts on. The band between them names
 * the hand-off in one direction.
 */
function ResultPanel({ evaluation }: { evaluation: JevDemoEvaluation }) {
  return (
    <section className="jev-result" aria-label="The evaluated result">
      <JudgmentPanel evaluation={evaluation} />

      <p className="jev-handoff">
        <span className="jev-handoff__arrow" aria-hidden="true">
          ↓
        </span>
        Those five Jev judgments are the only input to the step below. No model is asked again.
      </p>

      <PolicyPanel evaluation={evaluation} />
    </section>
  );
}

/** Half one: probabilities. Deliberately the only part in the model's colour. */
function JudgmentPanel({ evaluation }: { evaluation: JevDemoEvaluation }) {
  const { judgments } = evaluation;
  return (
    <div className="jev-result__half jev-result__half--model">
      <h2 className="jev-result__heading">
        What Jev judged <span className="badge badge--info">Probabilistic</span>
      </h2>
      <p className="small jev-result__lead">
        Five questions, one call. Three rated on a five-level rubric, two
        answered as a probability of yes. No decision anywhere in this half —
        answered by <code>{judgments.model_name}</code>.
      </p>

      <div className="jev-answers">
        <ScoreCard answer={judgments.workload_fairness} />
        <ScoreCard answer={judgments.preference_satisfaction} />
        <ScoreCard answer={judgments.overall_quality} />
        <NoulCard answer={judgments.overuse_concern} />
        <NoulCard answer={judgments.human_review_warranted} />
      </div>
    </div>
  );
}

function ScoreCard({ answer }: { answer: JevDemoScoreAnswer }) {
  const band = confidenceBand(answer.confidence);
  return (
    <div className="card jev-answer">
      <p className="jev-answer__question">{questionLabel(answer.question)}</p>
      <p className="jev-answer__value">{percent(answer.normalized)}</p>
      <p className="small jev-answer__reading">{scoreOutOf(answer)} on the rubric</p>

      <div
        className="jev-meter"
        role="img"
        aria-label={`${questionLabel(answer.question)}: ${percent(answer.normalized)} of the rubric`}
      >
        <span className="jev-meter__fill" style={{ width: percent(answer.normalized) }} />
      </div>

      <p className="small jev-answer__confidence">
        <span className={`badge badge--${band.tone}`}>{band.label}</span>
        <span className="muted">confidence {percent(answer.confidence)}</span>
      </p>

      {isDistributionSpread(answer) && (
        <p className="small muted jev-answer__spread">
          Probability is spread across levels rather than concentrated on one —
          which is what the confidence figure is reporting.
        </p>
      )}
    </div>
  );
}

function NoulCard({ answer }: { answer: JevDemoNoulAnswer }) {
  return (
    <div className="card jev-answer jev-answer--noul">
      <p className="jev-answer__question">{questionLabel(answer.question)}</p>
      <p className="jev-answer__value">{percent(answer.probability)}</p>
      <p className="small jev-answer__reading">
        probability of yes — {noulReading(answer)}
      </p>

      <div
        className="jev-meter"
        role="img"
        aria-label={`${questionLabel(answer.question)}: ${percent(answer.probability)} probability of yes`}
      >
        <span className="jev-meter__fill" style={{ width: percent(answer.probability) }} />
      </div>

      <p className="small muted jev-answer__confidence">
        A yes/no question carries no confidence figure: the probability is the
        whole answer, and 50% means “as likely as not”.
      </p>
    </div>
  );
}

/** Half two: the decision. Arithmetic, and shown as arithmetic. */
function PolicyPanel({ evaluation }: { evaluation: JevDemoEvaluation }) {
  const { policy } = evaluation;
  const tone = statusTone(policy.status);
  return (
    <div className="jev-result__half">
      <h2 className="jev-result__heading">
        What the application decided{" "}
        <span className="badge">Deterministic</span>
      </h2>
      <p className="small jev-result__lead">
        Ordinary Python, reading the five numbers above against thresholds this
        repository owns.
      </p>

      <div className={`jev-status jev-status--${tone}`} role="status">
        <p className="jev-status__label">{statusLabel(policy.status)}</p>
        <p className="jev-status__explanation">{statusExplanation(policy.status)}</p>
      </div>

      <div className="card jev-why">
        <p className="card__title">Why</p>
        {policy.reasons.length === 0 ? (
          <p className="muted">
            No policy rule fired. Every judgment above sits on the acceptable
            side of its threshold.
          </p>
        ) : (
          <ul className="jev-reasons">
            {policy.reasons.map((code) => {
              const threshold = reasonThreshold(code, policy.thresholds);
              return (
                <li key={code}>
                  <span className="jev-reasons__label">{reasonLabel(code)}</span>
                  <code className="jev-reasons__code">{code}</code>
                  {threshold !== null && (
                    <span className="small jev-reasons__threshold">
                      threshold {threshold}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        )}
        <p className="small jev-note">
          These rules are ordinary Python in{" "}
          <code>app/soft_constraints/policy.py</code>. Jev was never asked which
          status to pick — it was not even offered the choice.
        </p>
      </div>
    </div>
  );
}

function Stat({ label, value, alert }: { label: string; value: string; alert?: boolean }) {
  return (
    <li className={`stat${alert ? " stat--alert" : ""}`}>
      <p className="stat__value">{value}</p>
      <p className="stat__label">{label}</p>
    </li>
  );
}

/**
 * A failure, in the demo's own words.
 *
 * Deliberately not the app's shared `ErrorNotice`: that component explains
 * failures in terms of the scheduling API and offers to send somebody to sign
 * in, and neither sentence is true here. The commonest failure on this page is
 * "the demo server is not running", whose fix is one command.
 */
function DemoError({ error, title }: { error: unknown; title: string }) {
  const demoError = error instanceof JevDemoError ? error : null;

  if (demoError === null) {
    return (
      <div className="notice notice--danger" role="alert">
        <p className="notice__title">Something went wrong</p>
        <p>The page could not be displayed. Reloading may help.</p>
      </div>
    );
  }

  return (
    <div className="notice notice--danger" role="alert">
      <p className="notice__title">{demoError.kind === "unreachable" ? "The demo server is not running" : title}</p>
      {demoError.detail !== null && <p>{demoError.detail}</p>}
      <p className="small muted">{guidanceFor(demoError)}</p>
    </div>
  );
}

/** The next step, for the two failures somebody can actually fix from here. */
function guidanceFor(error: JevDemoError): string {
  switch (error.kind) {
    case "unreachable":
      return "Start it with: cd backend && python -m uvicorn app.main:app --reload --port 8000";
    case "evaluation_failed":
      return "The demo server needs TYPESAFE_API_KEY in its environment to run a live evaluation. The scenarios above still load without one.";
    case "unknown_scenario":
      return "Only the three scenarios listed above can be evaluated.";
    case "failed":
      return "Nothing was decided, and nothing was changed — this page changes nothing anywhere.";
  }
}
