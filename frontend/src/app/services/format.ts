/** 显示单位工具：内部数值统一 m³ / m，界面上换算为 万m³ 便于教学阅读。 */
const WAN = 10_000;

export function wan(m3: number, digits = 1): string {
  if (m3 === null || m3 === undefined || Number.isNaN(m3)) return '—';
  return (m3 / WAN).toLocaleString('zh-CN', {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
}

export function meters(m: number, digits = 2): string {
  return m === null || m === undefined ? '—' : m.toFixed(digits);
}

export function signedWan(m3: number, digits = 1): string {
  const s = wan(Math.abs(m3), digits);
  return m3 < 0 ? `-${s}` : s;
}
