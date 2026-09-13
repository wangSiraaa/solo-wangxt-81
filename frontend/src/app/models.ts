export interface Quant {
  value: number | null;
  unit: string;
}

export interface CurvePointIn {
  elevation: Quant;
  storage: Quant;
  area?: Quant | null;
}

export interface DemandIn {
  key: string;
  name: string;
  weight: number;
  amount: (Quant | null)[];
  min_fraction: number;
}

export interface StepIn {
  label?: string | null;
  duration: Quant;
  inflow?: Quant | null;
  net_evap_depth?: Quant | null;
  eco_flow?: Quant | null;
}

export interface ScenarioPayload {
  name: string;
  description?: string;
  curve: CurvePointIn[];
  dead_storage: Quant;
  max_storage: Quant;
  initial_storage: Quant;
  final_storage?: Quant | null;
  spillway_capacity?: Quant | null;
  steps: StepIn[];
  demands: DemandIn[];
}

export interface ScenarioMeta {
  id: string;
  name: string;
  description: string;
  builtin: boolean;
}

export interface SolveOptions {
  impute_inflow?: 'none' | 'linear' | 'mean' | 'zero';
  impute_demand?: 'none' | 'linear' | 'mean' | 'zero';
  weight_override?: Record<string, number> | null;
  final_storage?: Quant | null;
  spillway_capacity?: Quant | null;
  name?: string | null;
}

export interface Conflict {
  index: number;
  kind: string;
  message: string;
  storage_balance_m3: number;
  storage_bound_m3: number;
  gap_m3: number;
}

export interface LedgerRow {
  index: number;
  label: string | null;
  duration_s: number;
  start_storage_m3: number;
  inflow_m3: number;
  inflow_imputed: boolean;
  evaporation_m3: number;
  delivery_m3: Record<string, number>;
  delivery_total_m3: number;
  spill_m3: number;
  end_storage_m3: number;
  end_elevation_m: number;
  demand_m3: Record<string, number>;
  deficit_m3: Record<string, number>;
  eco_requirement_m3: number;
  eco_release_m3: number;
  eco_ok: boolean;
  conservation_residual_m3: number;
  fixed?: boolean;
  notice_locked?: boolean;
  commitment_m3?: Record<string, number>;
  shortfall_m3?: Record<string, number>;
}

export interface SolveResult {
  status: string;
  scenario_id?: string | null;
  scheme_id?: string | null;
  name: string;
  feasible: boolean;
  weights: Record<string, number>;
  imputed_inflow_indexes: number[];
  imputed_demand_indexes: Record<string, number[]>;
  impute_method: string;
  evap_iterations: number;
  objective: number | null;
  totals: Record<string, number>;
  storage_curve: { elevation_m: number; storage_m3: number; area_m2: number }[];
  level_m: number[];
  ledger: LedgerRow[];
  conflicts: Conflict[];
  demand_names: Record<string, string>;
}

export interface SchemeSummary {
  id: string;
  name: string;
  locked: boolean;
  result: SolveResult;
}

// ---------- 滚动计划 ----------

export interface ActualStep {
  step_index: number;
  label?: string | null;
  duration: Quant;
  inflow: Quant;
  net_evap_depth?: Quant | null;
  eco_flow?: Quant | null;
  delivery: Record<string, Quant>;
  spill?: Quant | null;
  measured_storage?: Quant | null;
}

export interface ForecastStep {
  step_index: number;
  label?: string | null;
  duration: Quant;
  inflow?: Quant | null;
  net_evap_depth?: Quant | null;
  eco_flow?: Quant | null;
}

export interface ScenarioForecast {
  key: string;
  name: string;
  probability: number | null;
  steps: ForecastStep[];
}

export interface Contract {
  notice_steps: number;
  down_cost_multiplier: number;
  emergency: boolean;
  emergency_note?: string;
}

export interface CurveRevision {
  points: { elevation: Quant; storage: Quant; area?: Quant | null }[];
  dead_storage: Quant;
  max_storage: Quant;
  convert_initial: boolean;
}

export interface ReplanRequestModel {
  scenario: ScenarioPayload;
  forecast_version: string;
  forecast: ForecastStep[];
  actual: ActualStep[];
  confirmed: { forecast_version: string; contract: Contract; result: SolveResult } | null;
  contract: Contract;
  impute_inflow: string;
  curve_revision: CurveRevision | null;
  evaluated_scenarios?: ScenarioForecast[] | null;
}

export interface Attribution {
  total_change_m3: Record<string, number>;
  due_to_forecast_m3: Record<string, number>;
  due_to_execution_m3: Record<string, number>;
  due_to_curve_m3: Record<string, number>;
  unexplained_m3: Record<string, number>;
  current_storage_gap_m3: number;
  current_storage_gap_due_to_execution_m3: number;
  current_storage_gap_due_to_revaluation_m3: number;
  actual_delivery_planned_m3: Record<string, number[]>;
  actual_delivery_m3: Record<string, number[]>;
  actual_execution_gap_m3: Record<string, number>;
  actual_inflow_planned_m3: number[];
  actual_inflow_m3: number[];
}

export interface ScenarioRange {
  key: string;
  name: string;
  probability: number | null;
  feasible: boolean;
  deficit_total_m3: Record<string, number>;
  deficit_range_per_period_m3: Record<string, number[]>;
  shortfall_total_m3: Record<string, number>;
  change_cost_total: number;
  final_storage_m3: number;
  conflicts_count: number;
}

export interface ReplanResult {
  plan: SolveResult;
  forecast_version: string;
  based_on_confirmed_version: string | null;
  prefix_len: number;
  horizon_len: number;
  contract: Contract & { effective_notice_steps: number };
  actual_ledger: any[];
  commitments_m3: Record<string, number[]>;
  reconciliations: { period: string; book_storage_m3: number; measured_storage_m3: number;
                     adjustment_m3: number; reason: string }[];
  attribution: Attribution;
  ranges: ScenarioRange[];
  probability_note: string;
  curve_revision_applied: boolean;
  stale_actual_warning?: string;
}

export interface Confirmation {
  id: string;
  scenario_id: string;
  forecast_version: string;
  actual_used_count: number;
  contract: Contract;
  result: SolveResult;
  created_at: string;
}
