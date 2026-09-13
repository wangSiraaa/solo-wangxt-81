import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../services/api.service';
import {
  ActualStep, Confirmation, Contract, ForecastStep, ReplanResult, ScenarioForecast,
  ScenarioMeta, ScenarioPayload,
} from '../../models';
import { ResultsPanelComponent } from '../../components/results-panel.component';
import { wan } from '../../services/format';

interface ActualRow {
  inflow_scale: number;       // 相对场景原始来水的倍数（1=按计划）
  delivery_scale: number;     // 各类供水相对计划的倍数（1=按计划，>1=超计划取水）
  measured_delta_wan: number | null;  // 实测库容相对账面的调整（万m³）
}

@Component({
  selector: 'app-rolling',
  standalone: true,
  imports: [CommonModule, FormsModule, ResultsPanelComponent],
  templateUrl: './rolling.page.html',
})
export class RollingPage implements OnInit {
  api = inject(ApiService);

  scenarios = signal<ScenarioMeta[]>([]);
  scenarioId = signal('');
  payload = signal<ScenarioPayload | null>(null);

  m = signal(3);                              // 已执行天数
  version = signal('v1');
  forecastScale = signal(1.0);                // 新预测来水倍数（骤降调 0.25）
  rows = signal<ActualRow[]>([]);
  contract = signal<Contract>({ notice_steps: 2, down_cost_multiplier: 10, emergency: false });
  reviseCurve = signal(false);
  evalWetDry = signal(true);

  confirmed = signal<Confirmation | null>(null);
  result = signal<ReplanResult | null>(null);
  busy = signal(false);
  error = signal('');
  warn = signal('');
  selectedPeriod = signal<number | null>(null);
  readonly fmtWan = wan;

  keys = computed(() => (this.payload()?.demands ?? []).map(d => d.key));
  n = computed(() => this.payload()?.steps.length ?? 0);

  ngOnInit() {
    this.api.scenarios().subscribe(list => {
      this.scenarios.set(list);
      const base = list.find(s => s.builtin && s.name.includes('常规')) ?? list[0];
      if (base) this.load(base.id);
    });
  }

  load(id: string) {
    this.scenarioId.set(id);
    this.api.scenario(id).subscribe(doc => {
      this.payload.set(doc.payload);
      this.api.confirmation(id).subscribe(c => this.confirmed.set(c));
      this.result.set(null);
      this.resetRows();
    });
  }

  resetRows() {
    this.rows.set(Array.from({ length: this.m() }, () =>
      ({ inflow_scale: 1, delivery_scale: 1, measured_delta_wan: null })));
  }

  setM(v: number) {
    this.m.set(v);
    const cur = this.rows();
    const next: ActualRow[] = [];
    for (let i = 0; i < v; i++) {
      next.push(cur[i] ?? { inflow_scale: 1, delivery_scale: 1, measured_delta_wan: null });
    }
    this.rows.set(next);
  }

  // ---- 一键教学情景 ----
  scenarioPlunge() { this.forecastScale.set(0.25); this.rows().forEach(r => r.inflow_scale = 1); }
  scenarioOverUse() {
    this.forecastScale.set(1.0);
    this.rows().forEach(r => { r.delivery_scale = 1.25; r.inflow_scale = 1; });
  }
  scenarioCurve() { this.forecastScale.set(1.0); this.reviseCurve.set(true); }

  private dur(t: number) {
    return this.payload()!.steps[t].duration;
  }

  private buildActual(plan: { ledger: any[] }, bookEndWan?: number | null): ActualStep[] {
    const p = this.payload()!;
    const hasMeasured = this.rows().some(r => r.measured_delta_wan !== null);
    return this.rows().map((r, t) => {
      const planned = plan.ledger[t];
      const delivery: Record<string, any> = {};
      for (const d of p.demands) {
        delivery[d.key] = {
          value: +(planned.delivery_m3[d.key] / 1e4 * r.delivery_scale).toFixed(4),
          unit: '万m3',
        };
      }
      // 实测库容 = 第一段无实测重算得到的"账面末库容" + 用户调整（只在最后一天）
      let measured: any = null;
      if (hasMeasured && t === this.m() - 1 && r.measured_delta_wan !== null
          && bookEndWan !== undefined && bookEndWan !== null) {
        measured = { value: +(bookEndWan + r.measured_delta_wan).toFixed(3), unit: '万m3' };
      }
      return {
        step_index: t, label: p.steps[t].label,
        duration: this.dur(t),
        inflow: { value: +(planned.inflow_m3 / 1e4 * r.inflow_scale).toFixed(4), unit: '万m3' },
        net_evap_depth: p.steps[t].net_evap_depth ?? null,
        eco_flow: p.steps[t].eco_flow ?? null,
        delivery,
        spill: planned.spill_m3 > 0
          ? { value: +(planned.spill_m3 / 1e4).toFixed(4), unit: '万m3' } : null,
        measured_storage: measured,
      };
    });
  }

  private buildForecast(scale: number): ForecastStep[] {
    const p = this.payload()!;
    const out: ForecastStep[] = [];
    for (let t = this.m(); t < this.n(); t++) {
      const st = p.steps[t];
      out.push({
        step_index: t, label: st.label, duration: st.duration,
        inflow: st.inflow ? { value: +(st.inflow.value! * scale).toFixed(4), unit: st.inflow.unit } : null,
        net_evap_depth: st.net_evap_depth ?? null,
        eco_flow: st.eco_flow ?? null,
      });
    }
    return out;
  }

  private curveRevision() {
    const p = this.payload()!;
    if (!this.reviseCurve()) return null;
    const f = 1.08;
    return {
      points: p.curve.map(pt => ({
        elevation: pt.elevation,
        storage: { value: +(pt.storage.value! * f).toFixed(3), unit: pt.storage.unit },
        area: pt.area ?? null,
      })),
      dead_storage: { value: +(p.dead_storage.value! * f).toFixed(3), unit: p.dead_storage.unit },
      max_storage: { value: +(p.max_storage.value! * f).toFixed(3), unit: p.max_storage.unit },
      convert_initial: true,
    };
  }

  private baselinePlan() {
    // 无确认时：先取已确认快照，否则用普通求解建立基线（v1）
    return new Promise<any>((resolve, reject) => {
      if (this.confirmed()) { resolve(this.confirmed()!.result); return; }
      this.api.validate(this.payload()!, {}).subscribe({ next: resolve, error: reject });
    });
  }

  private buildBody(plan: any, actual: ActualStep[], forecast: ForecastStep[],
                    scenarios: ScenarioForecast[] | null, revision: any) {
    return {
      scenario: this.payload()!,
      forecast_version: this.version(),
      forecast, actual,
      confirmed: this.confirmed()
        ? { forecast_version: this.confirmed()!.forecast_version,
            contract: this.confirmed()!.contract, result: this.confirmed()!.result }
        : null,
      contract: this.contract(),
      impute_inflow: 'none',
      curve_revision: revision,
      evaluated_scenarios: scenarios,
    };
  }

  replan() {
    this.busy.set(true); this.error.set(''); this.warn.set('');
    this.baselinePlan().then((plan: any) => {
      const forecast = this.buildForecast(this.forecastScale());
      const revision = this.curveRevision();
      let scenarios: ScenarioForecast[] | null = null;
      if (this.evalWetDry()) {
        scenarios = [
          { key: 'wet', name: '偏丰 +30%', probability: null, steps: this.buildForecast(this.forecastScale() * 1.3) },
          { key: 'base', name: '本次预测', probability: null, steps: forecast },
          { key: 'dry', name: '偏枯 −40%', probability: null, steps: this.buildForecast(this.forecastScale() * 0.6) },
        ];
      }
      const wantsMeasured = this.rows().some(r => r.measured_delta_wan !== null);
      // 第一段：不带实测库容，取得"按实际供水/入流记账"的账面末库容
      const dryActual = this.buildActual(plan, null).map(a => ({ ...a, measured_storage: null }));
      this.api.replan(this.buildBody(plan, dryActual, forecast, null, revision)).subscribe({
        next: dry => {
          if (!wantsMeasured) {
            this.finish(dry, scenarios);
            return;
          }
          const bookEndWan = dry.plan.ledger[this.m() - 1].end_storage_m3 / 1e4;
          const actual = this.buildActual(plan, bookEndWan);
          this.api.replan(this.buildBody(plan, actual, forecast, scenarios, revision)).subscribe({
            next: r => this.finish(r, scenarios),
            error: e => this.fail(e),
          });
        },
        error: e => this.fail(e),
      });
    }).catch(e => { this.busy.set(false); this.error.set('基线求解失败：' + e); });
  }

  private finish(r: ReplanResult, scenarios: ScenarioForecast[] | null) {
    // 区间已随主请求返回（第二段才带，避免第一段多算）
    this.result.set(r); this.busy.set(false);
  }

  private fail(e: any) {
    this.busy.set(false);
    this.error.set(e?.error?.detail?.message ?? '重算失败：' + (e?.message ?? e));
  }

  confirm() {
    const r = this.result();
    if (!r) return;
    this.api.confirm(this.scenarioId(), {
      scenario_id: this.scenarioId(),
      forecast_version: r.forecast_version,
      plan: r.plan, actual_used_count: r.prefix_len,
      contract: r.contract,
    }).subscribe(res => {
      if (res.warning) this.warn.set(res.warning);
      this.api.confirmation(this.scenarioId()).subscribe(c => this.confirmed.set(c));
    });
  }

  bumpVersion(v: string) {
    this.version.set(v);
  }

  setNotice(v: number) {
    this.contract.set({ ...this.contract(), notice_steps: v });
  }

  setMult(v: number) {
    this.contract.set({ ...this.contract(), down_cost_multiplier: v });
  }

  setEmergency(v: boolean) {
    this.contract.set({ ...this.contract(), emergency: v });
  }

  pickPeriod(i: number) {
    this.selectedPeriod.set(this.selectedPeriod() === i ? null : i);
  }
}
