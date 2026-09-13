// Wire contracts mirrored from backend/src/adoptimizer/schemas and domain/enums.
// Keep this file in sync with the backend; it is the single source of truth for
// the UI, so a drift here surfaces as a type error rather than a runtime surprise.

export type Platform = "google" | "meta" | "tiktok" | "mock";
export type CampaignStatus = "active" | "paused" | "completed" | "archived";
export type CreativeType = "text" | "image" | "video";
export type CreativeStatus = "draft" | "active" | "paused" | "rejected";
export type CreativeOrigin = "human" | "llm" | "rule";
export type AgentName = "monitor" | "audience" | "creative" | "bidding" | "optimize" | "critic";
export type RunStatus = "pending" | "running" | "succeeded" | "failed" | "cancelled";
export type Role = "admin" | "optimizer" | "analyst" | "viewer" | "ingestor";

export type ActionType =
  | "pause_creative"
  | "resume_creative"
  | "adjust_budget"
  | "adjust_bid"
  | "pause_campaign"
  | "resume_campaign"
  | "refresh_creative"
  | "start_ab_test"
  | "stop_ab_test"
  | "expand_audience";

export type ActionStatus =
  | "proposed"
  | "approved"
  | "rejected"
  | "executed"
  | "failed"
  | "skipped"
  | "suppressed";

export type AlertRule =
  | "low_ctr"
  | "high_cpa"
  | "low_roas"
  | "burn_rate"
  | "impression_collapse"
  | "frequency_fatigue";

export type AlertSeverity = "info" | "warning" | "critical";
export type AlertStatus = "open" | "acknowledged" | "resolved";
export type ABTestStatus = "draft" | "running" | "concluded" | "cancelled";

export type Permission =
  | "campaign:read"
  | "campaign:write"
  | "run:read"
  | "run:trigger"
  | "action:approve"
  | "action:execute"
  | "alert:read"
  | "alert:ack"
  | "creative:write"
  | "metrics:read"
  | "metrics:write"
  | "user:manage"
  | "audit:read"
  | "system:read";

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export interface User {
  id: string;
  email: string;
  full_name: string;
  role: Role;
  is_active: boolean;
  must_change_password: boolean;
  last_login_at: string | null;
  created_at: string | null;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: "bearer";
  expires_in: number;
  user: User;
}

export interface Campaign {
  id: string;
  name: string;
  platform: Platform;
  status: CampaignStatus;
  external_id: string | null;
  daily_budget: number;
  total_budget: number;
  target_cpa: number;
  target_roas: number;
  current_bid_cpm: number;
  start_date: string;
  end_date: string | null;
  objective: string;
  target_audience: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface CampaignCreate {
  name: string;
  platform: Platform;
  daily_budget: number;
  total_budget?: number;
  target_cpa?: number;
  target_roas?: number;
  start_date?: string | null;
  end_date?: string | null;
  objective?: string;
  target_audience?: string;
  external_id?: string | null;
}

export interface CampaignUpdate {
  name?: string;
  status?: CampaignStatus;
  daily_budget?: number;
  total_budget?: number;
  target_cpa?: number;
  target_roas?: number;
  end_date?: string | null;
  objective?: string;
  target_audience?: string;
  external_id?: string | null;
}

export interface Creative {
  id: string;
  campaign_id: string;
  headline: string;
  description: string;
  cta_text: string;
  creative_type: CreativeType;
  target_emotion: string;
  status: CreativeStatus;
  ab_group: string;
  origin: CreativeOrigin;
  score: number | null;
  generated_by_run_id: string | null;
  created_at: string | null;
}

export interface CreativeCreate {
  campaign_id?: string;
  headline: string;
  description?: string;
  cta_text?: string;
  creative_type?: CreativeType;
  target_emotion?: string;
  ab_group?: string;
  origin?: CreativeOrigin;
  status?: CreativeStatus;
}

export interface RunSummary {
  status: string;
  iterations: number;
  campaigns: number;
  creatives_generated: number;
  bidding_decisions: number;
  budget_adjustments: number;
  actions: number;
  actions_proposed: number;
  actions_suppressed: number;
  critic_findings: number;
  critic_findings_by_kind: Record<string, number>;
  action_counts: Record<string, number>;
  alerts_raised: number;
  health: Record<string, unknown>;
  usage: Record<string, unknown>;
}

export interface Run {
  id: string;
  status: RunStatus;
  trigger_type: string;
  requested_by: string | null;
  campaign_ids: string[];
  parameters: Record<string, unknown>;
  iteration: number;
  max_iterations: number;
  started_at: string | null;
  finished_at: string | null;
  error_message: string | null;
  summary: RunSummary | Record<string, unknown>;
  prompt_tokens: number;
  completion_tokens: number;
  llm_cost_usd: number;
  created_at: string | null;
}

export interface RunEvent {
  id: string;
  run_id: string;
  seq: number;
  agent: string;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string | null;
}

export interface OptimizationAction {
  id: string | null;
  run_id: string | null;
  campaign_id: string;
  creative_id: string | null;
  action_type: ActionType;
  status: ActionStatus;
  before_value: string;
  after_value: string;
  reason: string;
  confidence: number;
  proposed_by: string;
  // Which way spend moves and the reference frame it was judged against, so the
  // screen can explain why two proposals on one campaign contradict each other.
  direction: string;
  basis: Record<string, unknown>;
  created_at: string | null;
  approved_by: string | null;
  executed_at: string | null;
  external_reference: string | null;
  error_message: string | null;
}

export interface CriticFinding {
  id: string | null;
  run_id: string | null;
  iteration: number;
  kind: string;
  scope: string;
  campaign_id: string;
  creative_id: string | null;
  kept_action_id: string | null;
  kept_action_type: string;
  kept_confidence: number;
  reason: string;
  suppressed_action_ids: string[];
  suppressed_actions: Array<Record<string, unknown>>;
  // True when the critic refused to pick a winner and left the choice to a
  // human. Such a finding suppresses nothing; it flags a conflict.
  escalate: boolean;
  created_at: string | null;
}

export interface BudgetAllocation {
  campaign_id: string;
  campaign_name: string;
  current_budget: number;
  recommended_budget: number;
  change_pct: number;
  score: number;
  reason: string;
  solver: string;
}

export interface RunDetail {
  run: Run;
  events: RunEvent[];
  actions: OptimizationAction[];
  allocations: BudgetAllocation[];
  findings: CriticFinding[];
}

export interface Alert {
  id: string | null;
  campaign_id: string;
  run_id: string | null;
  rule: AlertRule;
  severity: AlertSeverity;
  status: AlertStatus;
  observed: number | null;
  threshold: number;
  message: string;
  dedup_key: string;
  context: Record<string, unknown>;
  detected_at: string | null;
  acknowledged_by: string | null;
  acknowledged_at: string | null;
}

export interface ABTest {
  id: string;
  campaign_id: string;
  name: string;
  hypothesis: string;
  status: ABTestStatus;
  control_creative_id: string | null;
  variant_creative_id: string | null;
  metric: string;
  minimum_detectable_effect: number;
  required_sample_size: number;
  traffic_split: number;
  started_at: string | null;
  concluded_at: string | null;
  winner_creative_id: string | null;
  result: Record<string, unknown>;
  created_at: string | null;
}

export interface AuditEntry {
  id: string;
  actor_id: string | null;
  actor_email: string;
  actor_role: string;
  action: string;
  resource_type: string;
  resource_id: string | null;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  ip_address: string;
  request_id: string;
  created_at: string | null;
}

export interface PerformanceSnapshot {
  campaign_id: string;
  campaign_name: string;
  impressions: number;
  clicks: number;
  conversions: number;
  total_cost: number;
  total_revenue: number;
  window_start: string | null;
  window_end: string | null;
  ctr: number;
  cvr: number;
  cpa: number | null;
  roas: number;
  cpc: number;
  cpm: number;
  predicted_ctr: number;
  predicted_cvr: number;
  ctr_lower_bound: number;
}

export interface PortfolioSummary {
  campaigns: number;
  impressions: number;
  clicks: number;
  conversions: number;
  total_cost: number;
  total_revenue: number;
  ctr: number;
  cvr: number;
  cpa: number | null;
  roas: number;
  health_score: number;
}

export interface RankedCampaign {
  campaign_id: string;
  campaign_name: string;
  roas: number;
  ctr: number;
  cvr: number;
  cpa: number | null;
  cost: number;
  revenue: number;
  impressions: number;
  conversions: number;
}

export interface OverviewResponse {
  window_days: number;
  portfolio: PortfolioSummary;
  health: { score: number; status: string };
  campaigns: { total: number; active: number; paused: number };
  alerts: {
    open: number;
    by_severity: Record<string, number>;
    acknowledged: number;
    resolved: number;
  };
  actions: {
    by_status: Record<string, number>;
    by_type: Record<string, number>;
    pending: number;
    executed: number;
  };
  runs: {
    by_status: Record<string, number>;
    total: number;
    active: number;
    latest_run_id: string | null;
    latest_run_at: string | null;
  };
  top_campaigns: RankedCampaign[];
  worst_campaigns: RankedCampaign[];
}

export interface TimeseriesPoint {
  date: string;
  impressions: number;
  clicks: number;
  conversions: number;
  cost: number;
  revenue: number;
  ctr: number;
  cvr: number;
  cpa: number | null;
  roas: number;
}

export interface CreativeStat {
  creative_id: string;
  impressions: number;
  clicks: number;
  conversions: number;
  cost: number;
  revenue: number;
  score: number;
  components: Record<string, number>;
}

export interface CampaignBreakdown {
  campaign_id: string;
  snapshot: PerformanceSnapshot | null;
  creatives: CreativeStat[];
}

export interface SpendModelRow {
  provider: string;
  model: string;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
}

export interface SpendSummary {
  window_days: number;
  by_model: SpendModelRow[];
  total_cost_usd: number;
  total_calls: number;
  monthly_budget_usd?: number;
  month_to_date_usd?: number;
}

export interface SystemInfo {
  environment: string;
  version: string;
  data_mode: string;
  llm_provider: string;
  llm_model: string;
  clickhouse_enabled: boolean;
  redis_enabled: boolean;
  database_dialect: string;
  require_action_approval: boolean;
  orchestrator_mode: string;
  cache_backend: string;
  uptime_seconds: number;
}

export interface HealthResponse {
  status: string;
  version: string;
  environment: string;
  uptime_seconds: number;
  dependencies: Record<string, Record<string, unknown>>;
}

export interface SeedResult {
  created_admin: boolean;
  campaigns: number;
  creatives: number;
  daily_rows: number;
  skipped: boolean;
}

export interface BulkSkippedEntry {
  id: string;
  reason: string;
}

export interface BulkFailedEntry {
  id: string;
  error: string;
}

export interface BulkActionResult {
  approved: string[];
  executed: string[];
  skipped: BulkSkippedEntry[];
  failed: BulkFailedEntry[];
}

export interface RunStartRequest {
  campaign_ids?: string[] | null;
  max_iterations?: number | null;
  window_days?: number;
  background?: boolean;
}

// Shape of one frame on the /runs/{id}/stream SSE channel.
export interface StreamFrame {
  id: string;
  run_id: string;
  seq: number;
  type: string;
  agent: string;
  payload: Record<string, unknown>;
  created_at: string;
}

// RFC 9457 problem detail, as emitted by core/errors.py.
export interface ProblemDetail {
  type: string;
  title: string;
  status: number;
  detail?: string;
  instance?: string;
  errors?: Array<{ loc: string; msg: string; type?: string }>;
  request_id?: string;
}