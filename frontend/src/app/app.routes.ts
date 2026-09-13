import { Routes } from '@angular/router';
import { SimulatorPage } from './pages/simulator/simulator.page';
import { RollingPage } from './pages/rolling/rolling.page';

export const routes: Routes = [
  { path: '', component: SimulatorPage },
  { path: 'rolling', component: RollingPage },
  { path: '**', redirectTo: '' },
];
