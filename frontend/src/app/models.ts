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
