import { Component, computed, input, output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ChartConfiguration } from 'chart.js';
import { ChartComponent } from './chart.component';
import { LedgerRow, SolveResult } from '../models';
import { meters, wan } from '../services/format';

const GRID = { color: 'rgba(255,255,255,.06)' };
const TICKS = { color: '#8aa0b8', font: { size: 11 } };
const PLUGIN_LEGEND = { labels: { color: '#dbe6f2', boxWidth: 12, font: { size: 11 } } };

@Component({
  selector: 'app-results-panel',
  standalone: true,
  imports: [CommonModule, ChartComponent],
  templateUrl: './results-panel.component.html',
})
export class ResultsPanelComponent {
  result = input.required<SolveResult>();
  selected = input<number | null>(null);
  pick = output<number>();

  readonly fmtWan = wan;
  readonly fmtM = meters;

  demandKeys = computed(() => Object.keys(this.result().demand_names));
  colors: Record<string, string> = {
    life: '#4da3ff', industry: '#b18cff', agriculture: '#3fbf7f',
  };
  colorOf(key: string) {
    return this.colors[key] ?? '#e0a04d';
  }

  private labels(): (string | null)[] {
    return this.result().ledger.map(r => r.label ?? `#${r.index + 1}`);
  }

  // 图1：库容（万m³）与水位（m，右轴）
  levelChart = computed<ChartConfiguration>(() => {
    const r = this.result();
    const storages = [r.ledger[0]?.start_storage_m3 ?? 0,
                      ...r.ledger.map(x => x.end_storage_m3)];
    return {
      type: 'line',
      data: {
        labels: ['期初', ...this.labels()],
        datasets: [
          {
            label: '蓄水量(万m³)',
            data: storages.map(v => +(v / 1e4).toFixed(3)),
            borderColor: '#4da3ff', backgroundColor: 'rgba(77,163,255,.15)',
            fill: true, tension: .2, yAxisID: 'y', pointRadius: 3,
          },
          {
            label: '水位(m)',
            data: r.level_m.map(v => +v.toFixed(3)),
            borderColor: '#f2b134', tension: .2, yAxisID: 'y1', pointRadius: 2,
          },
        ],
      },
      options: {
        responsive: true,
        onClick: (_e, el) => el.length && this.pick.emit(el[0].index - 1),
        scales: {
          x: { grid: GRID, ticks: TICKS },
          y: { position: 'left', grid: GRID, ticks: TICKS, title: { display: true, text: '万m³', color: '#8aa0b8' } },
          y1: { position: 'right', grid: { drawOnChartArea: false }, ticks: TICKS, title: { display: true, text: 'm', color: '#8aa0b8' } },
        },
        plugins: { legend: PLUGIN_LEGEND },
      },
    };
  });

  // 图2：水从哪来 / 到哪去（堆叠条，万m³）
  balanceChart = computed<ChartConfiguration>(() => {
    const r = this.result();
    const keys = this.demandKeys();
    return {
      type: 'bar',
      data: {
        labels: this.labels(),
        datasets: [
          { label: '入流', data: r.ledger.map(x => +(x.inflow_m3 / 1e4).toFixed(2)),
            backgroundColor: '#3d8fd1', stack: 'in' },
          { label: '蒸发', data: r.ledger.map(x => +(x.evaporation_m3 / 1e4).toFixed(2)),
            backgroundColor: '#c2703a', stack: 'out' },
          ...keys.map(k => ({
            label: `供水·${r.demand_names[k]}`,
            data: r.ledger.map(x => +((x.delivery_m3[k] ?? 0) / 1e4).toFixed(2)),
            backgroundColor: this.colorOf(k), stack: 'out',
          })),
          { label: '弃水', data: r.ledger.map(x => +(x.spill_m3 / 1e4).toFixed(2)),
            backgroundColor: '#8b93a7', stack: 'out' },
        ],
      },
      options: {
        responsive: true,
        onClick: (_e, el) => el.length && this.pick.emit(el[0].index),
        scales: {
          x: { stacked: true, grid: GRID, ticks: TICKS },
          y: { stacked: true, grid: GRID, ticks: TICKS, title: { display: true, text: '万m³', color: '#8aa0b8' } },
        },
        plugins: { legend: PLUGIN_LEGEND,
          tooltip: { callbacks: { label: c => `${c.dataset.label}: ${c.parsed.y} 万m³` } } },
      },
    };
  });

  // 图3：各类需求 vs 实际取水（缺口用红叠层直观看出）
  deliveryChart = computed<ChartConfiguration>(() => {
    const r = this.result();
    const keys = this.demandKeys();
    return {
      type: 'bar',
      data: {
        labels: this.labels(),
        datasets: keys.flatMap(k => [
          {
            label: `${r.demand_names[k]}·取水`,
            data: r.ledger.map(x => +((x.delivery_m3[k] ?? 0) / 1e4).toFixed(2)),
            backgroundColor: this.colorOf(k), stack: `d-${k}`,
          },
          {
            label: `${r.demand_names[k]}·缺口`,
            data: r.ledger.map(x => +((x.deficit_m3[k] ?? 0) / 1e4).toFixed(2)),
            backgroundColor: 'rgba(240,98,95,.85)', stack: `d-${k}`,
          },
        ]),
      },
      options: {
        responsive: true,
        onClick: (_e, el) => el.length && this.pick.emit(el[0].index),
        scales: {
          x: { stacked: true, grid: GRID, ticks: TICKS },
          y: { stacked: false, grid: GRID, ticks: TICKS, title: { display: true, text: '万m³', color: '#8aa0b8' } },
        },
        plugins: { legend: PLUGIN_LEGEND },
      },
    };
  });

  conflictCount = computed(() => this.result().conflicts.length);

  conservationOk = computed(() =>
    this.result().ledger.every(r => Math.abs(r.conservation_residual_m3) < 1e-2));

  conflictAt(index: number) {
    return this.result().conflicts.find(c => c.index === index);
  }

  imputedLabel(): string {
    return this.result().imputed_inflow_indexes
      .map(i => '#' + (i + 1)).join('、');
  }

  residual(v: number): string {
    return v.toExponential(2);
  }

  abs(v: number): number {
    return Math.abs(v);
  }
}
