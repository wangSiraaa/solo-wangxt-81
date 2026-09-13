import {
  AfterViewInit, Component, ElementRef, OnDestroy, ViewChild, effect, input,
} from '@angular/core';
import { ChartConfiguration } from 'chart.js';
import Chart from 'chart.js/auto';

@Component({
  selector: 'app-chart',
  standalone: true,
  template: `<canvas #canvas></canvas>`,
})
export class ChartComponent implements AfterViewInit, OnDestroy {
  @ViewChild('canvas') canvas!: ElementRef<HTMLCanvasElement>;
  config = input.required<ChartConfiguration>();
  private chart?: Chart;

  constructor() {
    effect(() => {
      const cfg = this.config();  // 建立信号依赖
      if (this.chart && this.canvas) {
        this.chart.destroy();
        this.chart = new Chart(this.canvas.nativeElement, cfg);
      }
    });
  }

  ngAfterViewInit() {
    this.chart = new Chart(this.canvas.nativeElement, this.config());
  }

  ngOnDestroy() {
    this.chart?.destroy();
  }
}
