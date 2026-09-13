import { Component, OnInit, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../services/api.service';
import { ScenarioMeta, ScenarioPayload, SchemeSummary, SolveOptions, SolveResult } from '../../models';
import { ResultsPanelComponent } from '../../components/results-panel.component';

@Component({
  selector: 'app-simulator',
  standalone: true,
  imports: [CommonModule, FormsModule, ResultsPanelComponent],
  templateUrl: './simulator.page.html',
})
export class SimulatorPage implements OnInit {
  api = inject(ApiService);

  scenarios = signal<ScenarioMeta[]>([]);
  selectedId = signal<string>('');
  payload = signal<ScenarioPayload | null>(null);
  weights = signal<Record<string, number>>({});
  impute = signal<'none' | 'linear' | 'mean' | 'zero'>('none');
  schemeName = signal('当前方案');

  active = signal<SolveResult | null>(null);
  locked = signal<SolveResult | null>(null);
  lockedMeta = signal<SchemeSummary | null>(null);
  savedSchemes = signal<SchemeSummary[]>([]);
  selectedPeriod = signal<number | null>(null);
  error = signal<string>('');
  busy = signal(false);

  ngOnInit() {
    this.api.scenarios().subscribe(list => {
      this.scenarios.set(list);
      const base = list.find(s => s.builtin && s.name.includes('常规')) ?? list[0];
      if (base) this.load(base.id);
    });
  }

  load(id: string) {
    this.selectedId.set(id);
    this.error.set('');
    this.api.scenario(id).subscribe(doc => {
      this.payload.set(doc.payload);
      const w: Record<string, number> = {};
      doc.payload.demands.forEach(d => (w[d.key] = d.weight));
      this.weights.set(w);
      this.active.set(null);
      this.locked.set(null);
      this.lockedMeta.set(null);
      this.refreshSchemes();
      this.solve();
    });
  }

  loadDemo(key: string) {
    this.api.demo(key).subscribe(p => {
      this.payload.set(p);
      const w: Record<string, number> = {};
      p.demands.forEach(d => (w[d.key] = d.weight));
      this.weights.set(w);
      this.active.set(null);
      this.locked.set(null);
      this.lockedMeta.set(null);
      this.selectedId.set('');
      this.solve();
    });
  }

  setWeight(key: string, value: number) {
    this.weights.update(w => ({ ...w, [key]: value }));
  }

  private options(name: string): SolveOptions {
    return {
      impute_inflow: this.impute(),
      impute_demand: this.impute(),
      weight_override: this.weights(),
      name,
    };
  }

  solve() {
    const p = this.payload();
    if (!p) return;
    this.busy.set(true);
    this.error.set('');
    const obs = this.selectedId()
      ? this.api.solve(this.selectedId(), this.options(this.schemeName()), true)
      : this.api.validate(p, this.options(this.schemeName()));
    obs.subscribe({
      next: r => {
        this.active.set(r);
        this.busy.set(false);
        if (this.selectedId()) this.refreshSchemes();
      },
      error: e => {
        this.busy.set(false);
        const d = e?.error?.detail;
        if (d?.missing_inflow?.length) {
          this.error.set(`时段 ${d.missing_inflow.map((i: number) => '#' + (i + 1)).join('、')} `
            + '来水缺测——缺测不能当作零。请在下方选择显式插补方式（linear/mean/zero）后再求解。');
        } else {
          this.error.set(d?.message ?? d?.errors?.[0]?.msg ?? '求解失败：' + (e?.message ?? e));
        }
      },
    });
  }

  lockActive() {
    const r = this.active();
    if (!r?.scheme_id) return;
    this.api.lock(r.scheme_id, true).subscribe(() => {
      this.locked.set(r);
      this.lockedMeta.set(this.savedSchemes().find(s => s.id === r.scheme_id) ?? null);
      this.schemeName.set('新方案（调整中）');
      this.refreshSchemes();
    });
  }

  unlockLocked() {
    const id = this.locked()?.scheme_id;
    if (!id) return;
    this.api.lock(id, false).subscribe(() => {
      this.locked.set(null);
      this.lockedMeta.set(null);
      this.refreshSchemes();
    });
  }

  useScheme(s: SchemeSummary) {
    this.active.set(s.result);
  }

  toggleLockSaved(s: SchemeSummary) {
    this.api.lock(s.id, !s.locked).subscribe(() => this.refreshSchemes());
  }

  refreshSchemes() {
    if (!this.selectedId()) return;
    this.api.schemes(this.selectedId()).subscribe(list => this.savedSchemes.set(list));
  }

  saveAsScenario() {
    const p = this.payload();
    if (!p) return;
    this.api.createScenario(p).subscribe(meta => this.load(meta.id));
  }

  pickPeriod(i: number) {
    this.selectedPeriod.set(this.selectedPeriod() === i ? null : i);
  }
}
