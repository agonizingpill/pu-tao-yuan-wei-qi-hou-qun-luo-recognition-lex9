import {orderStates, shiftStates, slots} from './domain.js';
import {safe, num, localTime, notice, title, table, paragraph} from './ui.js';


const reading = value => value === null || value === undefined ? '—' : num(value);

function baselineTable(judgment) {
  const baseline = judgment.baseline;
  if (!baseline?.samples?.length) return notice('前七天同班没有可用读数。');
  return table(['日期', '气温 °C', '湿度 %', '来源'], baseline.samples.map(s =>
    `<tr><td>${safe(s.observed_day.slice(5))}</td><td>${reading(s.temp_c)}</td><td>${reading(s.rh_pct)}</td><td class="muted">${safe(s.source)}</td></tr>`));
}

function evidenceBlock(item, accepted) {
  const j = item.judgment;
  const observed = j.observed;
  const offset = j.common_offset;
  return `<details class="evidence-fold" ${accepted ? 'open' : ''}>
    <summary>第 ${item.revision} 次判断 · ${shiftStates[item.state] || item.state} · ${safe(item.note)}
      <small>${localTime(item.created_at)} · ${safe(item.actor)}${accepted ? ' · 接单依据' : ''}</small></summary>
    <p class="muted">${safe(j.reason || '')}</p>
    <div class="evidence-grid">
      <div><h4>当时的读数</h4>
        <p>${observed ? `${reading(observed.temp_c)} °C · ${reading(observed.rh_pct)}% · <span class="muted">${safe(observed.source)}</span>` : '<span class="muted">该班缺读数</span>'}</p>
        <p class="muted">基期中位 ${reading(j.baseline.temp_c.median)} °C / ${reading(j.baseline.rh_pct.median)}%（${j.baseline.temp_c.n} 笔）</p>
        <p class="muted">全园同班偏移 ${offset ? `${num(offset.temp_c)} °C / ${num(offset.rh_pct)}%（${offset.sensors} 个探头）` : '不可算'}</p>
        <p class="muted">扣偏移后 ${j.adjusted ? `${num(j.adjusted.temp_c)} °C / ${num(j.adjusted.rh_pct)}%` : '—'} · 阈值 ${j.threshold ? `${num(j.threshold.temp_c)} °C / ${num(j.threshold.rh_pct)}%` : '—'}</p>
      </div>
      <div><h4>前七天同班基线读数</h4>${baselineTable(j)}</div>
    </div>
  </details>`;
}

function orderSheet(order, canWrite, supervisor) {
  const accepted = order.evidence.find(item => item.revision === order.accepted_revision);
  const latest = order.evidence[order.evidence.length - 1];
  const j = order.current_judgment;
  const canAccept = canWrite && order.state === 'open';
  return `<article class="round-sheet inspection-sheet">
    <span class="ref">${safe(order.block_code)} / ${safe(order.sensor)} · ${safe(order.observed_day)} ${slots[order.slot]}</span>
    <h3>${safe(order.block_name)}
      <span class="tag ${order.state === 'open' ? 'warm' : order.state === 'revoked' ? 'neutral' : 'warning'}">${orderStates[order.state]}</span></h3>
    <p>${safe(order.block_code)} 的探头 ${safe(order.sensor)} 在 ${safe(order.observed_day)} ${slots[order.slot]} 的判断。当前第 ${order.current_revision} 次判断${order.accepted_revision ? `，接单依据为第 ${order.accepted_revision} 次` : ''}。</p>
    ${j ? `<p class="muted">当前结论：${shiftStates[j.state] || j.state}${j.reason ? ` · ${safe(j.reason)}` : ''}</p>` : ''}
    ${order.state === 'accepted' ? `<p class="muted">${safe(order.accepted_by)} 于 ${localTime(order.accepted_at)} 接单，依据第 ${order.accepted_revision} 次判断。</p>` : ''}
    ${order.state === 'pending_review' ? notice(`晚到读数重算后结论已变为“${shiftStates[latest?.state] || latest?.state}”，原接单依据（第 ${order.accepted_revision} 次）保留，请主管复核。`, 'caution') : ''}
    ${order.state === 'revoked' ? notice(order.accepted_revision
      ? `复核后撤销了第 ${order.accepted_revision} 次依据的接单，证据仍保留。`
      : '重算后不再异常，无人接手的误报单已自动撤销。') : ''}
    ${canAccept ? `<form data-action="accept" data-id="${order.id}"><button class="primary" type="submit">接单并安排巡检</button></form>` : ''}
    ${order.state === 'pending_review' && supervisor ? `<form data-action="inspect-review" data-id="${order.id}">${paragraph('reason', '复核说明', '说明沿用或撤销原接单依据的理由')}
      <div class="actions"><button name="outcome" value="confirm" class="primary">维持接单</button>
      <button name="outcome" value="revoke" class="secondary">撤销接单</button></div></form>` : ''}
    ${canWrite ? `<form data-action="recompute-shift" class="inline-form">
      <input type="hidden" name="observed_day" value="${safe(order.observed_day)}">
      <input type="hidden" name="slot" value="${safe(order.slot)}">
      <button class="secondary" type="submit">按现有读数重算该班</button></form>` : ''}
    <h4>历次判断与用过的读数</h4>
    ${order.evidence.length ? [...order.evidence].reverse().map(item =>
      evidenceBlock(item, item.revision === order.accepted_revision)).join('') : notice('还没有判断证据。')}
  </article>`;
}

export function inspectionPage(board, selected, canWrite, supervisor) {
  const orders = board.orders;
  const active = selected || orders.find(o => o.state === 'open') || orders[0];
  const counts = orders.reduce((all, o) => ({...all, [o.state]: (all[o.state] || 0) + 1}), {});
  const rows = orders.map(o => `<tr class="${o.state === 'open' || o.state === 'pending_review' ? 'warn-row' : ''}">
    <td>${safe(o.observed_day)}</td><td>${slots[o.slot]}</td>
    <td><button class="text-button" data-inspect="${o.id}">${safe(o.block_code)} · ${safe(o.sensor)}</button></td>
    <td>${shiftStates[o.shift_state] || '—'}</td>
    <td>${orderStates[o.state]}</td></tr>`);
  const insufficient = board.shifts.filter(s => s.state === 'insufficient');
  const waiting = [...new Map(insufficient.map(s => [`${s.observed_day}/${s.slot}`, s])).values()]
    .slice(0, 8)
    .map(s => `<tr><td>${safe(s.observed_day)}</td><td>${slots[s.slot]}</td>
      <td>${insufficient.filter(x => x.observed_day === s.observed_day && x.slot === s.slot)
        .map(x => safe(x.block_code)).join('、')}</td></tr>`);
  return `${title('探头巡检', '按探头前七天同班读数拟合基线，扣除全园同班共同偏移后，剩余异常才开巡检单；样本不足的班次待补采。')}
    <div class="spread">
      <section>
        <div class="section-label"><h2>巡检单</h2><span>${['open', 'pending_review'].map(s => `${orderStates[s]} ${counts[s] || 0}`).join(' · ')}</span></div>
        ${rows.length ? table(['日期', '班次', '地块 / 探头', '当前判断', '巡检单'], rows)
          : notice('目前没有巡检单。异常读数会自动开单，全园共同降温只会更新判断。')}
        ${active ? orderSheet(active, canWrite, supervisor) : ''}
      </section>
      <section class="ledger">
        <h2>待补采班次</h2>
        <p class="muted">前七天同班读数不足三笔、或全园可比探头不足三个时，该班不开单，等待补采后自动重算。</p>
        ${waiting.length ? table(['日期', '班次', '待补采地块'], waiting) : notice('当前没有样本不足的班次。')}
        <h2>判断口径</h2>
        <p class="muted">每个探头用前七天同班读数取中位数作基线，离散度用 MAD。全园同班各探头残差的中位数作为共同偏移，先扣除再与 3σ 阈值（温度不低于 1.5 °C、湿度不低于 8%）比较。</p>
        <p class="muted">接单与重算并发时以先入账为准。接单后若重算转为正常则标待复核并保留原依据；无人接手的误报单自动撤销。同一探头同一班只保留一张巡检单。</p>
      </section>
    </div>`;
}
