import { Routes } from '@angular/router';
import { SimulatorPage } from './pages/simulator/simulator.page';

export const routes: Routes = [
  { path: '', component: SimulatorPage },
  { path: '**', redirectTo: '' },
];
