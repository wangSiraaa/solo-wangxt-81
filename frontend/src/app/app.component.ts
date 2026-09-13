import { Component } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [RouterOutlet, RouterLink, RouterLinkActive],
  template: `
    <nav style="display:flex; gap:18px; align-items:center; padding:10px 20px;
                background:var(--panel); border-bottom:1px solid var(--line)">
      <b style="color:var(--accent)">水库教学模拟</b>
      <a routerLink="/" routerLinkActive="active" [routerLinkActiveOptions]="{exact:true}"
         class="navlink">单次计划</a>
      <a routerLink="/rolling" routerLinkActive="active" class="navlink">滚动计划</a>
      <span class="muted small" style="margin-left:auto">不连接真实闸门 · 不替代现实供水决策</span>
    </nav>
    <router-outlet />
  `,
  styles: [`.navlink { color: var(--muted); padding: 4px 10px; border-radius: 6px; }
           .navlink.active, .navlink:hover { color: var(--text); background: var(--panel2); }`],
})
export class AppComponent {}
