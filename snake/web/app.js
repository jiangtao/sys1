const $ = id => document.getElementById(id);
const directions = {up: ['↑', '向上'], down: ['↓', '向下'], left: ['←', '向左'], right: ['→', '向右']};
const vectors = {up: [0, -1], down: [0, 1], left: [-1, 0], right: [1, 0]};
const modelLabel = model => model === 'jev' ? 'Jev' : model === 'kev' ? 'Kev' : 'Laya';
const activeModel = () => state?.model || $('model').value;
let stages = [];
let state = null, previous = null, gameId = null, running = false, busy = false, timer = null;
let moveAt = 0, records = [], filter = 'all', requestAt = 0, lastDecision = null, toastTimer, eventFlash = null;
const canvas = $('board'), ctx = canvas.getContext('2d');
let width = 0, height = 0;
const requestedModel = new URLSearchParams(location.search).get('model');
if (requestedModel === 'laya' || requestedModel === 'jev' || requestedModel === 'kev') $('model').value = requestedModel;

async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {'Content-Type': 'application/json'}, signal: AbortSignal.timeout(120000)});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : data.detail?.message || (typeof data.error === 'string' ? data.error : data.error?.message) || JSON.stringify(data.detail || data.error || data));
  return data;
}
function message(text, error = false) { $('message').textContent = text; $('message').classList.toggle('error', error); }
function setConnection(ready, device = '') { $('connection-dot').classList.toggle('ready', ready); $('connection').textContent = ready ? activeModel() === 'jev' ? 'JEV KEY READY' : `${modelLabel(activeModel()).toUpperCase()} ONLINE${device ? ' · ' + device.toUpperCase() : ''}` : `${modelLabel(activeModel()).toUpperCase()} OFFLINE`; }
function buttons() {
  $('toggle').disabled = !gameId || state?.status !== 'playing';
  $('step').disabled = !gameId || busy || running || state?.status !== 'playing';
  $('restart').disabled = busy || !gameId;
  $('model').disabled = busy;
  $('play-label').textContent = running ? '暂停运行' : '继续运行';
  $('play-icon').textContent = running ? 'Ⅱ' : '▷';
  $('run-status').textContent = state?.status === 'won' ? '挑战完成' : state?.status === 'lost' ? state?.lossReason === 'trapped' ? '无路可走 · 失败' : '生命耗尽 · 失败' : busy ? '● 路线评估中' : running ? '● 自主运行' : 'Ⅱ 已暂停';
  $('run-status').dataset.status = state?.status || 'starting';
  $('run-status').classList.toggle('busy', busy);
  document.body.classList.toggle('thinking', busy);
  $('thinking-label').textContent = busy ? `${modelLabel(activeModel())} 正在评估安全候选…` : state?.status === 'won' ? '所有关卡已完成' : running ? '决策已执行 · 等待下一步' : '已暂停 · 可单步观察';
}
function schedule() {
  clearTimeout(timer);
  const delay = $('speed').value === 'max' ? 0 : Math.max(25, (state?.intervalMs || 400) / Number($('speed').value));
  if (running && state?.status === 'playing') timer = setTimeout(step, delay);
}
function renderState(next) {
  if (Array.isArray(next.boardSize)) next = {...next, boardSize: next.boardSize[0]};
  previous = state && state.boardSize === next.boardSize && state.level === next.level ? state : null;
  state = next; moveAt = performance.now();
  $('model').value = state.model || 'laya';
  const label = modelLabel(state.model);
  $('brand-model').textContent = label.toUpperCase();
  $('intro-model').textContent = label;
  $('board-watermark').textContent = `${label.toUpperCase()} // NEURAL PLAYFIELD`;
  $('legend-model').textContent = label.toUpperCase();
  stages = state.levels || stages;
  const totalLevels = state.totalLevels || stages.length || 5;
  $('level').textContent = String(state.level).padStart(2, '0');
  $('total-levels').textContent = `/ ${String(totalLevels).padStart(2, '0')}`;
  $('challenge-count').textContent = `${totalLevels} 关挑战`;
  $('game-summary').textContent = `第 ${state.level}/${totalLevels} 关 · 收集 ${state.eaten}/${state.target}${state.controller === 'guarded' ? ' · 规划器兜底' : state.strategy ? state.strategy.policyMode === 'strict_shortest' ? ' · 最短优先' : ' · 存活优先' : ''}`;
  document.querySelector('.penalty-rule').textContent = state.strategy && state.strategy.policyMode !== 'strict_shortest' ? '无收益绕路 / 兜圈 → 扣血' : '绕路 −5 血 / −10 能量';
  document.querySelector('.penalty-rule').title = state.strategy && state.strategy.policyMode !== 'strict_shortest' ? '按已测指标有明确更好选择却无收益绕路，或重复完整局面，扣5生命、10能量；风险收益不确定时不凭猜测扣分。' : '已证明选择的路线更长时扣5生命、10能量；未知时不扣。';
  if (state.controller === 'guarded') {
    $('game-summary').title = `优先级：存活 > 最短路径 > 多吃能量 · seed=${state.seed ?? '—'}`;
    document.querySelector('.penalty-rule').textContent = '先校验再执行 · 纠正不扣血';
    document.querySelector('.penalty-rule').title = '模型更差或不可用时采用已验证的规划器路线；扣罚只针对实际执行的无效绕路。';
    document.querySelector('.log-footer').textContent = `● ${modelLabel(state.model)} 选择有取舍的方案 · 规划器执行确定步与兜底`;
  }
  const health = state.health ?? 100, stamina = state.energy ?? 100;
  $('health-text').textContent = `${health} / ${state.maxHealth || 100}`;
  $('stamina-text').textContent = `${stamina} / ${state.maxEnergy || 100}`;
  $('health-fill').style.width = `${Math.max(0,Math.min(100,health/(state.maxHealth || 100)*100))}%`;
  $('stamina-fill').style.width = `${Math.max(0,Math.min(100,stamina/(state.maxEnergy || 100)*100))}%`;
  $('energy').textContent = String(state.eaten).padStart(2, '0');
  $('target').textContent = `/ ${String(state.target).padStart(2, '0')}`;
  $('steps').textContent = String(state.steps).padStart(4, '0');
  $('arena-size').textContent = `${state.boardSize} × ${state.boardSize}`;
  const head = state.snake[0];
  $('coordinates').textContent = `X ${String(head[0]).padStart(2, '0')} : Y ${String(head[1]).padStart(2, '0')}`;
  $('levels').replaceChildren(...stages.map(({name, size, target}, i) => {
    const node = document.createElement('div'), done = state.status === 'won' || i + 1 < state.level, active = i + 1 === state.level;
    node.className = `level-card${done ? ' done' : active ? ' active' : ''}`;
    node.title = `第 ${i + 1} 关 · ${name} · ${size}×${size} · ${target} 个能量`;
    node.innerHTML = `<div class="level-card-top"><span>${String(i + 1).padStart(2, '0')}</span><span>${done ? '✓' : active ? '↗' : '·'}</span></div><h3>${name}</h3><div class="level-bar" style="width:${done ? 100 : active ? state.eaten / state.target * 100 : 0}%"></div>`;
    return node;
  }));
  if (state.status === 'won') {
    running = false;
    $('board-overlay').hidden = false;
    $('overlay-title').textContent = `${totalLevels} 关全部完成`;
    $('overlay-detail').textContent = `${state.steps} 步 · ${modelLabel(state.model)} 调用 ${records.filter(r => r.modelCalled === true).length} 次 · 收集 ${state.totalEaten} 个能量点`;
    message('挑战完成。可导出全部决策，或重新开启一次实验。');
  }
  if (state.status === 'lost') {
    running = false; $('board-overlay').hidden = false; $('board-overlay').dataset.outcome = 'lost';
    $('board-overlay').querySelector('.overlay-symbol').textContent = '×';
    $('overlay-title').textContent = state.lossReason === 'trapped' ? '无路可走，挑战失败' : '生命耗尽，挑战失败';
    $('overlay-detail').textContent = `第 ${state.level} 关 · ${state.steps} 次决策 · ${records.filter(r => r.penalty?.applied).length} 次${state.strategy ? '规则' : '绕路'}扣罚`;
    message(state.lossReason === 'trapped' ? '蛇已被困住，没有合法的下一步。本局结束，可重新挑战。' : '血量归零，本局已结束。点击重新开始可再试一次。',true);
  }
  buttons();
}
const tacticLabels = {forage:'追食',pursue_food:'追食',food:'追食',follow_tail:'腾出尾路',tail_release:'腾出尾路',tail:'腾出尾路',open_space:'保留空间',preserve_space:'保留空间',space:'保留空间'};
const tailLabel = value => value === true ? '尾路可达' : value === false ? '尾路不通' : '尾路未知';
function strategyView(d) {
  const options = d.strategyOptions || [], option = options.find(o => o.direction === d.executed) || {};
  const direction = directions[d.executed] || ['·','无路可走'];
  const tactic = tacticLabels[d.strategy?.chosenTactic] || d.strategy?.chosenTactic || '评估退路';
  const kind = d.gameOver && d.vitals?.after?.health === 0 ? 'lost' : d.penalty?.applied ? 'penalty' : d.levelCompleted ? 'level' : d.foodEaten ? 'food' : d.judgement?.kind === 'tradeoff' ? 'tradeoff' : 'tactic';
  const title = kind === 'lost' ? '本局结束，记录已保存' : kind === 'penalty' ? d.judgement?.kind === 'repeat' ? '重复兜圈，扣血' : `策略受罚：${d.penalty.healthDelta} 生命` : kind === 'level' ? `第 ${d.level} 关完成` : kind === 'food' ? '能量到手，继续前进' : `${modelLabel(d.model)} 选择${tactic}`;
  const measured = `食后出口 ${option.exitsAfterFood ?? '?'} 个，可达区 ${option.spaceAfterFood ?? '?'} 格，${tailLabel(option.tailReachableAfterFood)}。`;
  const explanation = kind === 'lost' && d.vitals?.terminalLoss?.reason === 'trapped' ? '这一步执行后已没有合法出口，蛇被困住；没有由引擎代替它改道。' : d.penalty?.applied ? d.penalty.reason || d.judgement?.reason : d.optimality?.reason || d.judgement?.reason || `${direction[1]}，${Number.isFinite(option.foodSteps) ? `当前食物路线 ${option.foodSteps} 步；` : '食物路径尚未求解；'}${measured}`;
  return {strategyMode:true,options,option,tactic,direction,kind,title,explanation,a:d.analysis || {},candidates:d.candidates || [],extra:0,saved:0};
}
function decisionView(d) {
  if (d.strategy && d.controller !== 'guarded') return strategyView(d);
  const a = d.analysis || {}, candidates = (d.candidates || []).filter(c => c.safe);
  const chosen = candidates.find(c => c.direction === d.executed), best = candidates.length ? Math.min(...candidates.map(c => c.distance)) : 0;
  const extra = a.extraDistance ?? (chosen ? chosen.distance - best : 0), saved = a.shortcutSaved || 0;
  const kind = d.penalty?.applied ? 'penalty' : d.routeOverride ? 'correction' : d.levelCompleted ? 'level' : d.foodEaten || d.ate ? 'food' : d.planning ? d.executionSource === 'planner_forced' ? 'forced' : 'efficient' : extra > 0 ? 'detour' : saved > 0 ? 'shortcut' : candidates.length === 1 ? 'forced' : 'efficient';
  const completed = d.levelCompleted?.level || d.levelCompleted;
  const routeSaving = d.planning && Number.isFinite(d.planning.proposedRemaining) ? d.planning.proposedRemaining - d.planning.plannedRemaining : null;
  const titles = {level:`第 ${completed} 关，拿下！`,food:'能量到手，蛇身 +1',shortcut:`抄近路！少走 ${saved} 格`,detour:`这步安全，还能省 ${extra} 格`,forced:'稳稳穿过，留住退路',efficient:d.planning?.proof === 'proved' ? '模型建议通过最短路校验' : '沿已验证的安全路线前进',correction:d.overrideKind === 'longer_proved' && routeSaving > 0 ? `引擎改道，少走 ${routeSaving} 步` : '引擎采用已验证路线',penalty:d.vitals?.after?.health === 0 ? '血量归零，本局失败' : `绕路受罚：${d.penalty?.healthDelta} 生命`};
  const direction = directions[d.executed] || ['·','未知'];
  let explanation = `${direction[1]}，剩余安全路线 ${chosen?.distance ?? '—'} 格。`;
  if (kind === 'shortcut') explanation = `${direction[1]}切入捷径，相比逐格沿安全回路走少 ${saved} 格。`;
  if (kind === 'detour') explanation = `${direction[1]}是安全的；另一个安全方向的剩余路线还可少 ${extra} 格。`;
  if (kind === 'forced') explanation = `只有${direction[1]}能保持安全，沿路前进，不挤掉退路。`;
  if (kind === 'food') explanation = `${direction[1]}正好吃到能量，接着寻找下一颗。`;
  if (kind === 'level') explanation = `本关能量收集完成，${d.level === (state?.totalLevels || 10) ? '全部挑战已完成。' : '下一关空间更紧、蛇身更长。'}`;
  if (d.planning && kind === 'efficient') explanation = d.planning.proof === 'proved' ? `${modelLabel(d.model)} 提议${direction[1]}，经完整蛇体规划确认：到当前能量点的最短合法路线。` : '搜索未能证明最短，先执行已经验证不会碰撞的路线。';
  if (kind === 'correction' || kind === 'penalty') explanation = d.overrideKind === 'longer_proved' ? `${modelLabel(d.model)} 提议${directions[d.chosen]?.[1]}需 ${d.planning.proposedRemaining} 步；最短 ${d.planning.plannedRemaining} 步。${kind === 'penalty' ? `扣 ${Math.abs(d.penalty.healthDelta)} 血、${Math.abs(d.penalty.energyDelta)} 能量，改道也不免罚。` : `规划器改为${direction[1]}。`}` : '模型提议未完成最优性证明，规划器保守执行已验证路线。';
  if (d.controller === 'guarded') {
    titles.efficient = d.planning?.proof === 'proved' ? `${modelLabel(d.model)} 提议通过路线校验` : '沿已验证的安全路线前进';
    titles.forced = '沿唯一安全方向前进';
    titles.correction = d.modelError ? `${modelLabel(d.model)} 调用失败，规划器接管` : '规划器兜底，避免走偏';
    if (d.routeOverride) {
      const proposed = directions[d.chosen]?.[1];
      explanation = d.modelError ? `${modelLabel(d.model)} 调用失败：${d.modelError}；规划器执行${direction[1]}。` : `${proposed ? `${modelLabel(d.model)} 提议${proposed}，` : ''}未通过安全路线校验；规划器执行${direction[1]}${Number.isFinite(d.planning?.plannedRemaining) ? `，沿 ${d.planning.plannedRemaining} 步路线前进` : ''}。`;
      explanation += d.penalty?.applied ? '实际执行触发扣罚，详见资源记录。' : '纠正后实际路线有效，不扣血。';
    } else if (kind === 'efficient') {
      explanation = d.planning?.proof === 'proved' ? `${modelLabel(d.model)} 提议${direction[1]}，达到当前安全约束内的最短步数；保留退路继续收集。` : '继续经过验证的安全路线；本步未证明最短。';
    }
    if (d.overrideKind === 'lookahead_preference') {
      titles.correction = '同长路线，选择更利于下一颗的方案';
      const future = d.planning.lookahead;
      explanation = `本颗都需 ${d.planning.plannedRemaining} 步。沿各自完整路线吃完后，下一颗平均步数 ${future.proposed.meanSteps.toFixed(2)} → ${future.executed.meanSteps.toFixed(2)}；规划器采用后者，不增加本颗路程。`;
    } else if (d.overrideKind === 'lookahead_unproved') {
      titles.correction = '后续成本未算清，保留已有路线';
      explanation = '当前路线同长，但新方案尚未证明吃完后的下一颗期望成本不变差；继续缓存的已验证完整路线。';
    }
    if (d.executionSource === 'planner_forced') {
      const uniqueBest = d.planning?.forcedReason === 'unique_proved_shortest';
      titles.forced = uniqueBest ? '唯一最短方向，规划器直接执行' : '唯一安全方向，规划器直接执行';
      explanation = uniqueBest ? `有 ${candidates.length} 个安全方向，但只有${direction[1]}达到当前食物的已证最短 ${d.planning.plannedRemaining} 步。规划器直接执行，未调用 ${modelLabel(d.model)}。` : `只有${direction[1]}满足安全约束，本步没有可比较的选择，未调用 ${modelLabel(d.model)}。`;
      if (d.foodEaten) explanation += '已收集能量点。';
    }
    const shortcut = d.planning?.cycleOptimization;
    if (shortcut?.switched) {
      return {a,candidates,chosen,best,extra,saved:shortcut.savedSteps,kind:'shortcut',
        title:`安全捷径，少走 ${shortcut.savedSteps} 步`,direction,
        explanation:`规划器把本颗能量的路线从 ${shortcut.baselineSteps} 步缩到 ${shortcut.selectedSteps} 步，完整路径和吃完后的退路均已校验。${d.routeOverride ? '本步采用规划器兜底。' : d.modelCalled ? `${modelLabel(d.model)} 提议通过校验。` : '本步由规划器直接执行。'}`};
    }
  }
  return {a,candidates,chosen,best,extra,saved,kind,title:titles[kind],explanation,direction};
}
function renderContext(d) {
  const context = d.contextAudit;
  if (!context) return;
  const complete = context.boardComplete && context.bodyComplete && context.knownRoutesComplete;
  const estimatedTokens = Number.isFinite(context.inputTokens);
  const rawTokens = Number.isFinite(context.rawInputTokens);
  const confirmed = !d.modelError && complete && context.tokenMatch !== false && (
    estimatedTokens ? context.inputTokens <= context.tokenBudget : d.model === 'kev' && rawTokens && context.rawInputTokens <= context.tokenBudget
  );
  $('context-note').hidden = false;
  const saving = context.savedInputTokens > 0 ? ` · 无损省 ${context.savedInputTokens}` : '';
  const tokenText = estimatedTokens ? `${context.inputTokens}/${context.tokenBudget} tokens` : `${d.model} usage ${rawTokens ? context.rawInputTokens : '?'} tokens`;
  $('context-note').textContent = confirmed ? `${complete ? '全棋盘 · 完整蛇身与已知路线' : '查看输入完整度'} · ${tokenText}${saving}` : '模型输入未确认 · 本步由规划器兜底';
  $('context-note').title = `近期 ${context.historyCount} 步动作；${context.unknownPlanIds?.length || 0} 条路线未证明最短。可展开原始输入核对。`;
}
function renderLive(d) {
  $('context-note').hidden = true;
  if (!d) {
    $('live-headline').textContent = `${modelLabel(activeModel())} 即将出发`; $('live-step').textContent = 'WAITING';
    $('live-explanation').textContent = '看它挑近路、吃能量，逐关突破。';
    $('choice-comparison').replaceChildren(); $('distance-change').replaceChildren(); $('proposal-comparison').replaceChildren();
    $('optimality-note').hidden = true;
    $('safe-total').textContent = '0 步'; $('saved-total').textContent = '0 步'; $('route-rate').textContent = '—';
    return;
  }
  const v = decisionView(d);
  renderContext(d);
  if (v.strategyMode) { renderStrategyLive(d,v); return; }
  $('optimality-note').hidden = true;
  $('metric-one-label').textContent = '安全执行'; $('metric-two-label').textContent = '引擎改道'; $('metric-three-label').textContent = '模型采纳';
  $('choice-comparison').classList.remove('strategy-options');
  $('live-decision').dataset.kind = v.kind;
  $('live-step').textContent = `#${String(d.step).padStart(4, '0')} · ${v.direction[0]} ${v.direction[1]}`;
  $('live-headline').textContent = v.title;
  $('live-explanation').textContent = v.explanation;
  $('planner-phase').textContent = d.planning ? ({PLAN_FOOD:'重新规划',FOLLOW_ROUTE:d.planning.proof === 'proved' ? '执行已验证最短路' : '安全回退 · 未证最短',DONE:'已通关',LOST:'生命耗尽'}[d.planning.phaseAfter] || '路线校验') : '最新一步';
  if (d.planning?.cycleOptimization?.switched) $('planner-phase').textContent = '规划器找到安全捷径';
  const proposal = directions[d.chosen];
  $('proposal-comparison').textContent = d.planning ? d.modelError ? `${modelLabel(d.model)} 调用失败  →  规划器执行 ${v.direction[0]} ${v.direction[1]}` : `${modelLabel(d.model)}${proposal ? `建议 ${proposal[0]} ${proposal[1]}` : '暂无有效提议'}  →  ${d.routeOverride ? '规划器执行' : '实际执行'} ${v.direction[0]} ${v.direction[1]}` : '';
  if (d.executionSource === 'planner_forced') $('proposal-comparison').textContent = `无需模型选择 · 规划器执行 ${v.direction[0]} ${v.direction[1]}`;
  $('choice-comparison').replaceChildren(...Object.keys(directions).map(direction => {
    const candidate = v.candidates.find(c => c.direction === direction), blocked = (v.a.blocked || []).find(c => c.direction === direction);
    const box = document.createElement('div');
    box.className = `choice-option ${candidate ? 'safe' : 'blocked'}${direction === d.executed ? ' selected' : ''}`;
    const plan = (d.candidatePlans || []).find(p => p.direction === direction);
    const label = candidate ? Number.isFinite(plan?.routeSteps) ? `${plan.routeSteps} 步${plan.proof === 'proved' ? '' : '?'}` : `环距 ${candidate.distance}` : ({wall:'撞墙',body:'碰蛇身',food:'越过食物环序',order:'破坏安全环序'}[blocked?.kind] || '不安全');
    box.innerHTML = `<span>${directions[direction][0]} <b>${directions[direction][1]}</b></span><small></small>`;
    box.querySelector('small').textContent = direction === d.executed ? `${label} ✓` : label;
    if (plan?.nextFood?.proof === 'proved') box.querySelector('small').textContent += ` · 下颗均 ${plan.nextFood.meanSteps.toFixed(1)}`;
    box.title = blocked?.reason || (plan ? `${plan.id || '方案'} 决策链：${(plan.fullRoute || plan.chain || []).map(step => directions[step]?.[0] || step).join(' ')}；${plan.proof === 'proved' ? '此方向后续最短步数已验证' : '最优性未证明'}` : candidate ? '环距是回路上的参考距离，不代表实际最短移动步数' : '不满足安全约束');
    if (plan?.nextFood?.proof === 'proved') box.title += `；沿此完整路线进食后，枚举全部 ${plan.nextFood.targetCount} 个下一能量位置，平均 ${plan.nextFood.meanSteps.toFixed(2)} 步。不是整局最优证明。`;
    return box;
  }));
  const distance = $('distance-change'); distance.replaceChildren();
  if (d.planning || Number.isFinite(v.a.distanceBefore) && Number.isFinite(v.a.distanceAfter)) {
    const label = document.createElement('span'); label.textContent = d.planning ? d.planning.proof === 'proved' ? '本颗能量最短路 · 限当前安全环序' : '安全回退路线 · 未证明最短' : '参考环距（不是最短步数）';
    const value = document.createElement('strong'); value.textContent = d.planning ? `${d.planning.plannedRemaining} → ${d.planning.executedRemaining} 步` : `${v.a.distanceBefore} → ${v.a.distanceAfter} 格`;
    distance.append(label,value);
  }
  $('safe-total').textContent = `${records.length} 步`;
  const overrides = records.filter(r => r.routeOverride).length;
  $('saved-total').textContent = `${overrides} 步`;
  $('route-rate').textContent = `${records.length - overrides}/${records.length}`;
  if (d.controller === 'guarded') {
    const proposals = records.filter(r => r.modelCalled === true && !r.modelError && new Set((r.candidatePlans || []).map(p => p.direction)).size > 1);
    const changed = proposals.filter(r => !r.routeOverride && r.executed !== r.planning?.plannedDirection).length;
    $('metric-two-label').textContent = '规划器兜底'; $('metric-three-label').textContent = `${modelLabel(d.model)} 改选`;
    $('route-rate').textContent = `${changed}/${proposals.length}`;
    $('route-rate').title = `真正调用 ${modelLabel(d.model)} 后，改选了与规划器参考方向不同的同长安全路线。这个数字不是正确率；整局收益要与纯规划器对照。`;
  }
}
function renderStrategyLive(d,v) {
  $('metric-one-label').textContent = '模型决策'; $('metric-two-label').textContent = '受罚决策'; $('metric-three-label').textContent = '已收集';
  $('safe-total').textContent = `${records.length} 步`;
  $('saved-total').textContent = `${records.filter(r => r.penalty?.applied).length} 次`;
  $('route-rate').textContent = `${records.filter(r => r.foodEaten).length}`;
  $('live-decision').dataset.kind = v.kind;
  $('planner-phase').textContent = d.strategy.phaseAfter === 'LOST' ? '挑战结束' : `${modelLabel(d.model)} 自主选策略`;
  $('live-step').textContent = `#${String(d.step).padStart(4,'0')} · ${v.direction[0]} ${v.direction[1]}`;
  $('live-headline').textContent = v.title;
  const audit = d.optimality;
  $('optimality-note').hidden = !audit;
  if (audit) {
    const proved = ['metrics_best','food_shortest'].includes(audit.status);
    $('optimality-note').dataset.status = audit.status;
    $('optimality-note').textContent = `${proved ? '✓' : audit.status === 'not_best' ? '×' : '·'} ${audit.label}`;
    $('optimality-note').title = `${audit.scope}。${audit.reason}`;
  }
  $('live-explanation').textContent = v.explanation;
  $('proposal-comparison').textContent = `模型选择 ${v.direction[0]} ${v.direction[1]} · 已实际执行`;
  const container = $('choice-comparison'); container.classList.add('strategy-options');
  container.replaceChildren(...v.options.map(option => {
    const box = document.createElement('div');
    box.className = `strategy-option${option.direction === d.executed ? ' selected' : ''}`;
    const dir = document.createElement('strong'); dir.textContent = `${directions[option.direction]?.[0]} ${directions[option.direction]?.[1]}${option.direction === d.executed ? ' ✓' : ''}`;
    const cost = document.createElement('span'); cost.textContent = Number.isFinite(option.foodSteps) ? `食物 ${option.foodSteps} 步` : '食物路径 ?';
    const exit = document.createElement('span'); exit.textContent = `食后出口 ${option.exitsAfterFood ?? '?'}`;
    const tail = document.createElement('span'); tail.textContent = tailLabel(option.tailReachableAfterFood);
    const space = document.createElement('span'); space.textContent = `可达区 ${option.spaceAfterFood ?? '?'}`;
    box.append(dir,cost,exit,tail,space);
    box.title = `第一步后：出口 ${option.exitsAfter ?? '?'}、可达区 ${option.spaceAfter ?? '?'}、${tailLabel(option.tailReachableAfter)}；重复状态 ${option.revisitCountAfter || 0} 次。可达区含头部及可腾空的尾格，食后指标不保证长期存活。`;
    if (Array.isArray(option.fullRoute)) box.title += ` 完整候选路线：${option.fullRoute.map(move => directions[move]?.[0] || move).join(' ')}。每步仍由 ${modelLabel(d.model)} 重新选择。`;
    return box;
  }));
  const label = document.createElement('span'); label.textContent = '尾路、出口、空区为风险指标';
  const value = document.createElement('strong'); value.textContent = `未进食 ${d.strategy.noFoodSteps ?? 0} 步`;
  $('distance-change').replaceChildren(label,value);
}
function logNode(decision) {
  const v = decisionView(decision), node = document.createElement('article'); node.className = `log-item event-${v.kind}`;
  const stamp = new Date(decision.timestamp).toLocaleTimeString('zh-CN', {hour12:false});
  node.innerHTML = `<div class="log-meta"><span class="log-index">#${String(decision.step).padStart(4,'0')}</span><span>· 第 ${decision.level} 关</span><time></time></div><div class="log-action"><span class="direction-icon"></span><strong></strong><span></span></div><p></p><div class="log-tags"></div><details><summary>查看路线对比与模型原始输出</summary><pre></pre></details>`;
  node.querySelector('time').textContent = stamp;
  node.querySelector('.direction-icon').textContent = {food:'◆',level:'⚑',shortcut:'↗',detour:'↪'}[v.kind] || v.direction[0];
  node.querySelector('strong').textContent = v.title;
  node.querySelector('.log-action>span:last-child').textContent = `${Math.round(decision.latencyMs)} ms`;
  node.querySelector('p').textContent = v.explanation;
  const tags = ['✓ 无碰撞', v.direction[1]];
  if (v.strategyMode) tags.push(`策略 ${v.tactic}`);
  else if (decision.planning?.proof === 'proved') tags.push('✓ 环序内最短路已校验');
  else if (!decision.planning && v.extra === 0 && v.candidates.length > 1) tags.push('最短候选环距');
  if (decision.modelCallStatus === 'failed' || decision.modelError) tags.push(`${modelLabel(decision.model)} 调用失败 · 规划器接管`);
  else if (decision.modelCallStatus === 'succeeded') tags.push(`${modelLabel(decision.model)} 已真实调用`);
  if (decision.routeOverride) tags.push(`模型 ${directions[decision.chosen]?.[0] || '无有效提议'} → 规划器 ${v.direction[0]}`);
  if (decision.executionSource === 'planner_forced') tags.push('规划器确定步 · 未调用模型');
  if (decision.planning?.forcedReason === 'unique_proved_shortest') tags.push('唯一已证最短方向');
  if (decision.planning?.lookahead?.active) tags.push('同长路线前瞻');
  if (decision.planning?.cycleOptimization?.switched) tags.push(`安全捷径 −${decision.planning.cycleOptimization.savedSteps} 步`);
  if (decision.optimality) tags.push(decision.optimality.label);
  if (decision.contextAudit) tags.push(`输入 ${decision.contextAudit.inputTokens} tokens`);
  if (decision.penalty?.applied) tags.push(`♥ ${decision.vitals?.before?.health} → ${decision.vitals?.after?.health}`,`◆ ${decision.vitals?.before?.energy} → ${decision.vitals?.after?.energy}`);
  if (decision.foodEaten) tags.push('能量点 +1');
  if (decision.vitals?.foodRecovery?.health > 0) tags.push(`进食回血 +${decision.vitals.foodRecovery.health}`);
  if (decision.vitals?.levelRecovery?.health > 0) tags.push(`过关回血 +${decision.vitals.levelRecovery.health}`);
  if (v.candidates.length === 1) tags.push(v.strategyMode ? '唯一可执行方向' : '唯一安全方向');
  if (!decision.planning && v.saved > 0) tags.push(`参考环距 −${v.saved} 格`);
  for (const text of tags) { const tag = document.createElement('span'); tag.textContent = text; node.querySelector('.log-tags').append(tag); }
  node.querySelector('pre').textContent = JSON.stringify({model:decision.model,controller:decision.controller,executionSource:decision.executionSource,modelCalled:decision.modelCalled,modelCallStatus:decision.modelCallStatus,modelError:decision.modelError,modelChoice:decision.modelChoice,choiceMap:decision.choiceMap,chosen:decision.chosen,executed:decision.executed,strategy:decision.strategy,strategyOptions:decision.strategyOptions,judgement:decision.judgement,optimality:decision.optimality,contextAudit:decision.contextAudit,routeOverride:decision.routeOverride,overrideKind:decision.overrideKind,planning:decision.planning,penalty:decision.penalty,vitals:decision.vitals,candidatePlans:decision.candidatePlans,analysis:decision.analysis,candidates:decision.candidates,confidence:decision.confidence,request:decision.request,raw:decision.raw},null,2);
  if (decision.contextAudit) {
    const details = document.createElement('details'), summary = document.createElement('summary'), input = document.createElement('pre');
    summary.textContent = `查看 ${modelLabel(decision.model)} 看到的棋盘与完整路线`;
    input.textContent = typeof decision.request?.state === 'string' ? decision.request.state : decision.request?.state?.context || JSON.stringify(decision.request?.state,null,2);
    details.append(summary,input);
    node.insertBefore(details,node.querySelector('details'));
  }
  return node;
}
function isEvent(d) { return d.foodEaten || d.levelCompleted || ['food','level','shortcut','correction','penalty','tradeoff','lost'].includes(decisionView(d).kind); }
function renderLogs() {
  const matching = records.filter(d => filter === 'all' || isEvent(d));
  $('logs').replaceChildren(...matching.slice(-150).reverse().map(logNode));
  if (!matching.length) { const empty = document.createElement('div'); empty.className = 'empty-log'; empty.textContent = filter === 'events' ? '正在接近能量，精彩走位马上就来。' : '等待第一次决策…'; $('logs').append(empty); }
  $('log-count').textContent = records.length;
}
function addLog(decision) {
  records.push(decision); $('log-count').textContent = records.length;
  renderLive(decision);
  const container = $('logs'), atTop = container.scrollTop < 30;
  if (filter === 'events' && !isEvent(decision)) return;
  container.querySelector('.empty-log')?.remove();
  const node = logNode(decision); container.prepend(node);
  if (!atTop) container.scrollTop += node.offsetHeight;
  while (container.childElementCount > 150) container.lastElementChild.remove();
}
async function step() {
  if (busy || !gameId || state.status !== 'playing') return;
  busy = true; requestAt = performance.now(); buttons();
  try {
    const result = await api(`/api/game/${gameId}/step`, {method: 'POST', body: JSON.stringify({expectedStep: state.steps})});
    setConnection(!result.decision?.modelError);
    const before = state;
    lastDecision = result.decision;
    if (lastDecision) {
      lastDecision.ate = result.state.totalEaten > before.totalEaten;
      addLog(lastDecision);
      const view = decisionView(lastDecision);
      if (view.kind === 'food' || view.kind === 'shortcut') eventFlash = {at:performance.now(),point:lastDecision.analysis?.foodBefore || before.food,level:before.level,text:view.kind === 'food' ? '+1 能量' : `捷径 −${view.saved} 步`,food:view.kind === 'food'};
      $('latency').textContent = Math.round(lastDecision.latencyMs);
      if (lastDecision.levelCompleted && result.state.status === 'playing') {
        $('level-toast').textContent = `第 ${result.state.level} 关 · 难度升级`;
        $('level-toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('level-toast').hidden = true, 1800);
      }
    }
    renderState(result.state);
    if (state.status === 'playing') message(`第 ${state.level} 关 · 目标 ${state.target} 个能量 · 最小步间隔 ${state.intervalMs} ms（另加模型推理耗时）`);
    if (lastDecision?.modelError && ['jev', 'kev'].includes(state.model)) { running = false; message(`${modelLabel(state.model)} 决策失败，已暂停：${lastDecision.modelError}`, true); }
    if (result.warning) { running = false; message(`已暂停：${result.warning}。本步已执行，请先修复日志存储。`,true); }
  } catch (error) {
    running = false; setConnection(false);
    // Reconcile a request that may have finished after the connection dropped.
    // Never silently lose a server-side decision or repeat a stale step.
    try {
      const snapshot = await api(`/api/game/${gameId}`);
      const history = await api(`/api/game/${gameId}/logs`);
      records = history.logs || []; renderState(snapshot.state); renderLogs(); renderLive(records.at(-1));
    } catch { /* The retained state remains visible until the user retries. */ }
    message(`决策暂停：${error.message}。恢复服务后点“继续运行”重试。`, true);
  } finally { busy = false; buttons(); schedule(); }
}
async function newGame({reuseSeed = false} = {}) {
  if (busy) return;
  clearTimeout(timer); running = false; busy = true; buttons();
  try {
    const oldId = gameId;
    const requestedSeed = new URLSearchParams(location.search).get('seed');
    const parsedSeed = requestedSeed !== null && /^\d{1,10}$/.test(requestedSeed) ? Number(requestedSeed) : NaN;
    const seed = reuseSeed && state ? state.seed : Number.isSafeInteger(parsedSeed) && parsedSeed >= 0 && parsedSeed < 2 ** 32 ? parsedSeed : Math.floor(Math.random() * 2147483647);
    const model = $('model').value;
    const health = await api(`/api/health?model=${model}`);
    if (health.status !== 'ready' && health.status !== 'ok') throw new Error(`${modelLabel(model)} 尚未就绪`);
    const result = await api('/api/game', {method: 'POST', body: JSON.stringify({seed, model})});
    gameId = result.id; state = null; previous = null; records = []; lastDecision = null; eventFlash = null;
    $('board-overlay').hidden = true; $('board-overlay').dataset.outcome = ''; $('board-overlay').querySelector('.overlay-symbol').textContent = '✦'; $('level-toast').hidden = true; $('latency').textContent = '—';
    renderState(result.state); renderLogs(); renderLive(null); running = true;
    if (oldId) api(`/api/game/${oldId}`, {method: 'DELETE'}).catch(() => {});
    setConnection(health.status === 'ready' || health.status === 'ok', health.device);
    message(`${modelLabel(model)} 已接管方向。安全约束先筛选候选，模型逐步决策。`);
    if (result.warning) { running = false; message(`已暂停：${result.warning}`,true); }
  } catch (error) { $('model').value = state?.model || 'laya'; message(`无法创建游戏：${error.message}。请检查模型配置后重试。`, true); }
  finally { busy = false; buttons(); schedule(); }
}
$('toggle').addEventListener('click', () => { running = !running; clearTimeout(timer); buttons(); if (running && !busy) step(); });
$('step').addEventListener('click', step);
$('restart').addEventListener('click', () => newGame());
$('model').addEventListener('change', () => newGame({reuseSeed:true}));
const retryButton = document.createElement('button');
retryButton.className = 'primary retry-button'; retryButton.textContent = '重新挑战';
retryButton.addEventListener('click', () => newGame()); $('board-overlay').append(retryButton);
$('speed').addEventListener('change', () => { if (!busy) schedule(); });
document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => { filter = button.dataset.filter; document.querySelectorAll('[data-filter]').forEach(item => item.classList.toggle('active', item === button)); renderLogs(); }));
$('export').addEventListener('click', async () => {
  if (!gameId) return;
  try {
    const logs = await api(`/api/game/${gameId}/logs`);
    const blob = new Blob([JSON.stringify({gameId, model:state.model, exportedAt: new Date().toISOString(), state, logs}, null, 2)], {type: 'application/json'});
    const url = URL.createObjectURL(blob), anchor = document.createElement('a'); anchor.href = url; anchor.download = `${state.model || 'laya'}-snake-${gameId}.json`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    message('完整决策日志已导出。侧栏仅渲染最近 150 条以保持流畅。');
  } catch (error) { message(`导出失败：${error.message}`, true); }
});

function resize() {
  const rect = canvas.getBoundingClientRect(); width = rect.width; height = rect.height;
  const dpr = Math.min(window.devicePixelRatio || 1, 2); canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
new ResizeObserver(resize).observe(canvas);
function rounded(x, y, w, h, r) { ctx.beginPath(); ctx.roundRect(x, y, w, h, r); ctx.fill(); }
function draw(now) {
  ctx.clearRect(0, 0, width, height);
  const n = state?.boardSize || 14, size = Math.min(width - 46, height - 44), cell = size / n, ox = (width - size) / 2, oy = (height - size) / 2;
  const pulse = .5 + .5 * Math.sin(now / 600);
  ctx.strokeStyle = '#203126'; ctx.lineWidth = .65;
  for (let i = 0; i <= n; i++) { ctx.beginPath(); ctx.moveTo(ox + i * cell, oy); ctx.lineTo(ox + i * cell, oy + size); ctx.stroke(); ctx.beginPath(); ctx.moveTo(ox, oy + i * cell); ctx.lineTo(ox + size, oy + i * cell); ctx.stroke(); }
  ctx.fillStyle = '#3f5540'; ctx.font = '7px monospace'; ctx.textAlign = 'center';
  for (let i = 0; i < n; i += 2) { ctx.fillText(String(i).padStart(2, '0'), ox + (i + .5) * cell, oy - 7); ctx.fillText(String(i).padStart(2, '0'), ox - 12, oy + (i + .5) * cell + 2); }
  if (state) {
    const t = Math.min(1, (now - moveAt) / 170), eased = 1 - Math.pow(1 - t, 3);
    const positions = state.snake.map((point, index) => { const old = previous?.snake[Math.min(index, previous.snake.length - 1)] || point; return [old[0] + (point[0] - old[0]) * eased, old[1] + (point[1] - old[1]) * eased]; });
    if (state.food) {
      const [fx, fy] = state.food, x = ox + (fx + .5) * cell, y = oy + (fy + .5) * cell;
      ctx.strokeStyle = `rgba(255,149,100,${.12 + pulse * .05})`; ctx.lineWidth = .8; ctx.beginPath(); ctx.arc(x, y, cell * (.28 + pulse * .02), 0, Math.PI * 2); ctx.stroke();
      ctx.save(); ctx.translate(x, y); ctx.rotate(Math.PI / 4); ctx.fillStyle = '#ff996b'; ctx.shadowColor = '#ff804c'; ctx.shadowBlur = 9 + pulse * 4; rounded(-cell * .145, -cell * .145, cell * .29, cell * .29, 2); ctx.restore();
    }
    for (const [x,y] of state.obstacles || []) { ctx.fillStyle = '#465747'; rounded(ox+x*cell+2,oy+y*cell+2,cell-4,cell-4,3); }
    // A single connected spine keeps the snake fluid between model decisions.
    ctx.lineCap = 'round'; ctx.lineJoin = 'round'; ctx.lineWidth = cell * .4;
    if (positions.length > 1) {
      ctx.strokeStyle = '#79ac48'; ctx.shadowColor = '#b5f268'; ctx.shadowBlur = 3; ctx.beginPath();
      positions.forEach(([x,y],i) => i ? ctx.lineTo(ox+(x+.5)*cell,oy+(y+.5)*cell) : ctx.moveTo(ox+(x+.5)*cell,oy+(y+.5)*cell)); ctx.stroke(); ctx.shadowBlur = 0;
    }
    for (let i = positions.length - 1; i >= 0; i--) {
      const [x,y] = positions[i], pad = cell * .28;
      ctx.fillStyle = i === 0 ? '#c2ff81' : `hsl(${88 + i / positions.length * 10} 52% ${62 - i / positions.length * 25}%)`;
      if (i === 0) { ctx.shadowColor = '#a5fa65'; ctx.shadowBlur = 6; }
      rounded(ox+x*cell+pad,oy+y*cell+pad,cell-2*pad,cell-2*pad,cell*.12); ctx.shadowBlur = 0;
    }
    const head = positions[0], dir = vectors[state.direction] || [1,0], perp = [-dir[1], dir[0]];
    ctx.fillStyle = '#17321a';
    for (const sign of [-1,1]) { const x = ox+(head[0]+.5+dir[0]*.10+perp[0]*.09*sign)*cell, y = oy+(head[1]+.5+dir[1]*.10+perp[1]*.09*sign)*cell; ctx.beginPath(); ctx.arc(x,y,Math.max(.8,cell*.032),0,Math.PI*2);ctx.fill(); }
    // Small steady chevrons show current safe directions without inference-time blinking.
    for (const c of (state.candidates || []).filter(c => c.safe)) {
      const v = vectors[c.direction], side = [-v[1], v[0]], scale = Math.min(3,cell*.1);
      const x = ox+(head[0]+.5+v[0]*.75)*cell, y = oy+(head[1]+.5+v[1]*.75)*cell;
      ctx.strokeStyle='#9dcc6860'; ctx.lineWidth=1; ctx.beginPath();
      ctx.moveTo(x-v[0]*scale+side[0]*scale,y-v[1]*scale+side[1]*scale); ctx.lineTo(x,y); ctx.lineTo(x-v[0]*scale-side[0]*scale,y-v[1]*scale-side[1]*scale); ctx.stroke();
    }
    if (eventFlash && eventFlash.level === state.level && now-eventFlash.at < 700) {
      const age=(now-eventFlash.at)/700, point=eventFlash.point;
      ctx.save(); ctx.globalAlpha=(1-age)*.85; ctx.fillStyle=eventFlash.food?'#ffb18a':'#b5f268'; ctx.font='11px monospace'; ctx.textAlign='center';
      ctx.fillText(eventFlash.text,ox+(point[0]+.5)*cell,oy+(point[1]+.1)*cell-age*10); ctx.restore();
    }
  }
  // Subtle scanner across the playfield.
  const scanY = oy + (now / 90 % size); ctx.fillStyle = '#b5f26803'; ctx.fillRect(ox, scanY, size, 2);
  if (busy) $('thinking-time').textContent = `${((now - requestAt) / 1000).toFixed(1)} s`; else $('thinking-time').textContent = '';
  requestAnimationFrame(draw);
}
requestAnimationFrame(draw);
async function boot() {
  try { const health = await api(`/api/health?model=${$('model').value}`); const ready = health.status === 'ready' || health.status === 'ok'; setConnection(ready, health.device); if (!ready) message('模型尚未就绪，游戏会在请求失败时暂停。', true); }
  catch (error) { setConnection(false); message(`模型连接失败：${error.message}`, true); }
  await newGame();
}
boot();
