"use client";

import { Waypoints } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/cva.config";

export interface RoutingDecisionTierBoundaries {
  simple_medium?: number;
  medium_complex?: number;
  complex_reasoning?: number;
}

export interface RoutingDecision {
  router_model_name?: string;
  router_type?: string;
  routed_model?: string;
  cause?: string;
  tier?: string;
  score?: number;
  signals?: string[];
  matched_keyword?: string;
  escalation_keyword?: string;
  classifier_model?: string;
  escalated?: boolean;
  tier_boundaries?: RoutingDecisionTierBoundaries;
}

const ROUTER_TYPE_LABELS: Record<string, string> = {
  complexity: "Auto-Router v2",
  semantic: "Semantic router",
  adaptive: "Adaptive router",
  quality: "Quality router",
};

const REASONING_OVERRIDE_SIGNAL = "reasoning-override";

/**
 * The tier the score alone would have produced, given the boundaries in effect when
 * the decision was made. Rendered as the bracket that explains a score, so it must
 * use the snapshot rather than today's config.
 */
function describeScoreAgainstBoundaries(score: number, boundaries?: RoutingDecisionTierBoundaries): string | null {
  if (!boundaries) return null;
  const {
    simple_medium: simpleMedium,
    medium_complex: mediumComplex,
    complex_reasoning: complexReasoning,
  } = boundaries;
  if (simpleMedium === undefined || mediumComplex === undefined || complexReasoning === undefined) return null;

  if (score < simpleMedium) return `below ${simpleMedium}, SIMPLE`;
  if (score < mediumComplex) return `${simpleMedium} to ${mediumComplex}, MEDIUM`;
  if (score < complexReasoning) return `${mediumComplex} to ${complexReasoning}, COMPLEX`;
  return `at or above ${complexReasoning}, REASONING`;
}

function describeCause(decision: RoutingDecision): string {
  const { cause, classifier_model: classifierModel, matched_keyword: matchedKeyword, signals } = decision;
  const isReasoningOverride = Boolean(signals?.includes(REASONING_OVERRIDE_SIGNAL));

  switch (cause) {
    case "heuristic_scorer":
      return isReasoningOverride ? "Heuristic, REASONING override (2 or more reasoning markers)" : "Heuristic scorer";
    case "llm_classifier":
      return classifierModel ? `LLM classifier (${classifierModel})` : "LLM classifier";
    case "literal_keyword_match":
      return matchedKeyword ? `Keyword match: "${matchedKeyword}"` : "Keyword match";
    case "semantic_keyword_match":
      return "Semantic keyword match";
    case "session_affinity_pin":
      return "Pinned to session";
    case "session_affinity_escalation":
      return "Escalated from session pin";
    case "semantic_route":
      return "Semantic route match";
    case "quality_tier":
      return "Quality tier mapping";
    case "keyword":
      return matchedKeyword ? `Keyword match: "${matchedKeyword}"` : "Keyword match";
    case "bandit":
      return "Adaptive bandit";
    case "default_fallback":
      return "Default model, no route matched";
    default:
      return cause ?? "Unknown";
  }
}

/**
 * A request can ask to escalate and get nowhere, when its tier is already the highest
 * one configured. That row still has to say the caller asked, otherwise it reads as an
 * ordinary route; it just must not claim a bump that did not happen.
 */
function describeEscalation(escalated?: boolean, keyword?: string): string {
  if (escalated) return keyword ? `Yes, keyword "${keyword}"` : "Yes";
  return keyword ? `Requested via "${keyword}"; already at the highest tier` : "No";
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-3 py-1 text-sm">
      <span className="w-28 shrink-0 text-muted-foreground">{label}</span>
      <span className="min-w-0 break-words">{children}</span>
    </div>
  );
}

export function RoutingDecisionCard({
  decision,
  className,
}: {
  decision?: RoutingDecision | null;
  className?: string;
}) {
  if (!decision || !decision.cause) return null;

  const {
    router_model_name: routerModelName,
    router_type: routerType,
    routed_model: routedModel,
    tier,
    score,
    signals,
    escalated,
    escalation_keyword: escalationKeyword,
    tier_boundaries: tierBoundaries,
  } = decision;

  const isReasoningOverride = Boolean(signals?.includes(REASONING_OVERRIDE_SIGNAL));
  // On an override row the score did not decide the tier, so showing it against a
  // boundary would claim something untrue.
  const scoreExplanation =
    score !== undefined && !isReasoningOverride ? describeScoreAgainstBoundaries(score, tierBoundaries) : null;

  return (
    <div className={cn("mb-6 w-full max-w-full overflow-hidden rounded-lg bg-white shadow-sm", className)}>
      <div className="border-b px-4 py-2.5 text-sm font-medium">Routing</div>
      <div className="px-4 py-3">
        {routerModelName && (
          <div className="mb-2 flex items-center gap-2 text-sm font-medium">
            <Waypoints size={14} aria-hidden />
            <span>{routerModelName}</span>
            {routerType && (
              <span className="font-normal text-muted-foreground">
                ({ROUTER_TYPE_LABELS[routerType] ?? routerType})
              </span>
            )}
          </div>
        )}

        {tier && (
          <Row label="Tier">
            <Badge variant="secondary" className="font-normal">
              {tier}
            </Badge>
          </Row>
        )}

        <Row label="Decided by">{describeCause(decision)}</Row>

        {score !== undefined && (
          <Row label="Score">
            <span className="tabular-nums">{score.toFixed(2)}</span>
            {scoreExplanation && <span className="ml-2 text-muted-foreground">({scoreExplanation})</span>}
          </Row>
        )}

        {routedModel && <Row label="Routed to">{routedModel}</Row>}

        {(escalated || escalationKeyword) && (
          <Row label="Escalated">{describeEscalation(escalated, escalationKeyword)}</Row>
        )}

        {signals && signals.length > 0 && (
          <Row label="Signals">
            <span className="flex flex-wrap gap-1">
              {signals.map((signal) => (
                <Badge key={signal} variant="outline" className="font-normal">
                  {signal}
                </Badge>
              ))}
            </span>
          </Row>
        )}
      </div>
    </div>
  );
}

export default RoutingDecisionCard;
