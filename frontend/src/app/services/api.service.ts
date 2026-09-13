import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { ScenarioMeta, ScenarioPayload, SchemeSummary, SolveOptions, SolveResult } from '../models';

@Injectable({ providedIn: 'root' })
export class ApiService {
  private http = inject(HttpClient);
  readonly base = '/api';

  health() {
    return this.http.get<{ ok: boolean; units: Record<string, string[]>; notice: string }>(
      `${this.base}/health`);
  }

  demos() {
    return this.http.get<{ key: string; name: string; description: string }[]>(
      `${this.base}/demos`);
  }

  demo(key: string) {
    return this.http.get<ScenarioPayload>(`${this.base}/demos/${key}`);
  }

  scenarios() {
    return this.http.get<ScenarioMeta[]>(`${this.base}/scenarios`);
  }

  scenario(id: string) {
    return this.http.get<{ id: string; name: string; description: string; builtin: boolean; payload: ScenarioPayload }>(
      `${this.base}/scenarios/${id}`);
  }

  createScenario(payload: ScenarioPayload) {
    return this.http.post<ScenarioMeta>(`${this.base}/scenarios`, payload);
  }

  cloneScenario(id: string) {
    return this.http.post<ScenarioMeta>(`${this.base}/scenarios/${id}/clone`, {});
  }

  validate(scenario: ScenarioPayload, options: SolveOptions = {}) {
    return this.http.post<SolveResult>(`${this.base}/validate`, { scenario, options });
  }

  solve(scenarioId: string, options: SolveOptions, save = true) {
    return this.http.post<SolveResult>(
      `${this.base}/scenarios/${scenarioId}/solve?save=${save}`, options);
  }

  schemes(scenarioId: string) {
    return this.http.get<SchemeSummary[]>(`${this.base}/scenarios/${scenarioId}/schemes`);
  }

  lock(schemeId: string, locked: boolean) {
    return this.http.post(`/api/schemes/${schemeId}/lock?locked=${locked}`, {});
  }

  compare(scenarioId: string, options: SolveOptions[]) {
    return this.http.post<{ scenario_id: string; schemes: SolveResult[] }>(
      `${this.base}/scenarios/${scenarioId}/compare`, options);
  }
}
