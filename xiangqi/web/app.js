const $ = (id) => document.getElementById(id);
const SVG = 'http://www.w3.org/2000/svg';
const names = { K:'帅', A:'仕', B:'相', E:'相', N:'马', H:'马', R:'车', C:'炮', P:'兵', k:'将', a:'士', b:'象', e:'象', n:'马', h:'马', r:'车', c:'炮', p:'卒' };
let game = null, selected = null, flipped = false, busy = false, filter = 'tree', puzzles = [], timer = null, treeIndex = -1;
let autoPlaying=false,autoEpoch=0,autoTimer=null,decisionSide='all',chosenPuzzle=null,difficulty='all',branchPreview=null;
let storageAvailable = true;
let serviceHealth = null;
const initialEmpty = $('logs').innerHTML;
const supportedModels=['laya','jev','kev'];
const providerLabels={laya:'Laya',jev:'Jev',kev:'Kev'};
function normalizedModel(model){return supportedModels.includes(model)?model:'laya';}
function providerName(model){return providerLabels[normalizedModel(model)];}
function providerUpper(model){return providerName(model).toUpperCase();}
function selectedModel(){return normalizedModel(game?.model);}
function oppositeModel(model){return supportedModels.find(candidate=>candidate!==normalizedModel(model))||'laya';}
function isDuel(){return game?.mode==='duel';}
function sideModel(side){return isDuel()&&side==='black'?(game?.opponentModel||oppositeModel(selectedModel())):selectedModel();}
function logModel(log){return normalizedModel(log.model||log.kind);}
function modelChoice(log){return log.modelChoice||log.layaChoice;}
function modelResponse(log){return log.raw?.[logModel(log)]||log.raw?.laya||log.raw?.jev||log.raw?.kev||log.raw;}

function element(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined && text !== null) el.textContent = String(text);
  return el;
}
function svg(tag, attrs = {}, text) {
  const el = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
  if (text !== undefined) el.textContent = text;
  return el;
}
function storeGame(id) { try { sessionStorage.setItem('laya-xiangqi-game', id); } catch { storageAvailable = false; } }
function storedGame() { try { return sessionStorage.getItem('laya-xiangqi-game'); } catch { storageAvailable = false; return null; } }
function restoreSearchTime() {
  try {
    const saved=localStorage.getItem('laya-xiangqi-search-seconds-fast');
    if([...$('think-time').options].some(option=>option.value===saved))$('think-time').value=saved;
  } catch { /* Without storage, keep the fast default for this page. */ }
}
function readableFailure(text) {
  if(typeof text!=='string')return String(text??'未知错误');
  // Older servers wrap the model response in a string and may truncate its tail.
  // A complete JSON message field is still useful even if later details are cut.
  const match=text.match(/"message"\s*:\s*("(?:[^"\\]|\\.)*")/);
  if(match){try{return JSON.parse(match[1]);}catch{}}
  return text;
}
async function api(path, body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 90000);
  try {
    const response = await fetch(path, { method: body ? 'POST' : 'GET', headers: body ? {'Content-Type':'application/json'} : {}, body: body ? JSON.stringify(body) : undefined, signal:controller.signal });
    const data = await response.json();
    if (!response.ok) {
      const reason=typeof data.error === 'string' ? data.error : data.error?.message || data.detail || `请求失败（${response.status}）`;
      const error=new Error(['laya_unavailable','jev_unavailable','kev_unavailable','model_unavailable'].includes(data.error?.code)?readableFailure(reason):reason);
      error.details=data.error?.details;
      throw error;
    }
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('决策等待超时，请稍后重试。');
    throw error;
  } finally { clearTimeout(timeout); }
}
function message(text, error = false) { $('message').textContent = text; $('message').classList.toggle('error', error); }
function isRed(piece) { return piece && piece === piece.toUpperCase(); }
function square(row, col) { return String.fromCharCode(97 + col) + (9 - row); }
function coords(sq) { return [9 - Number(sq[1]), sq.charCodeAt(0) - 97]; }
function position(row, col) { return { x: 48 + (flipped ? 8 - col : col) * 55.5, y: 48 + (flipped ? 9 - row : row) * 56 }; }
function parseBoard(fen) {
  return fen.split(' ')[0].split('/').map(row => Array.from(row).flatMap(p => /[1-9]/.test(p) ? Array(Number(p)).fill(null) : [p]));
}
function boardState() { return branchPreview?.board || (game?.fen ? parseBoard(game.fen) : Array.from({length:10}, () => Array(9).fill(null))); }
function legalMoves() { return game?.legalMoves || []; }
function allLogs() { const seen=new Set();return [...(game?.archiveLogs||[]),...(game?.logs||[])].filter(log=>{if(log.id&&seen.has(log.id))return false;if(log.id)seen.add(log.id);return true;}); }
function lastMove() { if(branchPreview)return branchPreview.move;const last = game?.moves?.at(-1); return typeof last === 'string' ? last : last?.move || game?.logs?.filter(x=>x.move).at(-1)?.move; }
function isPlaying() { return game?.status === 'playing'; }
function attachActivation(el, callback) {
  el.addEventListener('click', callback);
  el.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); callback(event); } });
}

function renderBoard() {
  const board = $('board'); board.replaceChildren();
  board.setAttribute('aria-label', `象棋棋盘，${flipped ? '黑' : '红'}方在下。${game?.turn === 'red' ? '轮到红方' : '轮到黑方'}。`);
  const defs = svg('defs');
  const grad = svg('radialGradient', {id:'piece-fill',cx:'40%',cy:'30%',r:'70%'});
  grad.append(svg('stop',{offset:'0%','stop-color':'#fff5dd'}),svg('stop',{offset:'100%','stop-color':'#ecddbd'}));
  const shadow=svg('filter',{id:'piece-shadow',x:'-30%',y:'-30%',width:'170%',height:'180%'});
  shadow.append(svg('feDropShadow',{dx:'0',dy:'2',stdDeviation:'1.6','flood-color':'#53452b','flood-opacity':'.25'}));
  defs.append(grad,shadow); board.append(defs);
  board.append(svg('rect',{x:43,y:43,width:454,height:514,class:'board-outer'}));
  for(let r=0;r<10;r++) board.append(svg('line',{x1:48,y1:48+r*56,x2:492,y2:48+r*56,class:'board-grid'}));
  for(let c=0;c<9;c++) {
    const x=48+c*55.5;
    if(c===0 || c===8) board.append(svg('line',{x1:x,y1:48,x2:x,y2:552,class:'board-grid'}));
    else {board.append(svg('line',{x1:x,y1:48,x2:x,y2:272,class:'board-grid'}));board.append(svg('line',{x1:x,y1:328,x2:x,y2:552,class:'board-grid'}));}
    board.append(svg('text',{x,y:580,'text-anchor':'middle',class:'coordinate'},String.fromCharCode(97+(flipped?8-c:c))));
  }
  for(let r=0;r<10;r++)board.append(svg('text',{x:23,y:52+r*56,'text-anchor':'middle',class:'coordinate'},String(flipped?r:9-r)));
  for(const base of [48,440])for(const [a,b] of [[214.5,325.5],[325.5,214.5]])board.append(svg('line',{x1:a,y1:base,x2:b,y2:base+112,class:'board-grid'}));
  board.append(svg('text',{x:151,y:309,'text-anchor':'middle',class:'river-label'},'楚 河'),svg('text',{x:392,y:309,'text-anchor':'middle',class:'river-label'},'汉 界'));
  for(const [r,c] of [[2,1],[2,7],[7,1],[7,7],...[3,6].flatMap(r=>[0,2,4,6,8].map(c=>[r,c]))]) {
    const {x,y}=position(r,c);
    for(const dx of [-1,1])for(const dy of [-1,1]) {
      if((c===0&&dx===(flipped?1:-1))||(c===8&&dx===(flipped?-1:1)))continue;
      board.append(svg('path',{d:`M ${x+dx*4} ${y+dy*13} L ${x+dx*4} ${y+dy*4} L ${x+dx*13} ${y+dy*4}`,class:'board-grid'}));
    }
  }
  const last = lastMove();
  if(last && /^[a-i][0-9][a-i][0-9]$/.test(last))for(const sq of [last.slice(0,2),last.slice(2)]) {
    const {x,y}=position(...coords(sq));
    board.append(svg('rect',{x:x-24,y:y-24,width:48,height:48,rx:5,class:'last-move-mark'}));
  }
  const pieces=boardState();
  const targets=selected ? legalMoves().filter(m=>m.startsWith(selected)).map(m=>m.slice(2)) : [];
  for(let r=0;r<10;r++)for(let c=0;c<9;c++) {
    const sq=square(r,c),p=pieces[r][c],{x,y}=position(r,c);
    if(!p) {
      const hit=svg('g',{class:'move-target',role:'button',tabindex:targets.includes(sq)?0:-1,'aria-label':targets.includes(sq)?`落子到 ${sq}`:`空位 ${sq}`});
      hit.append(svg('circle',{cx:x,cy:y,r:25,fill:'transparent'}));
      if(targets.includes(sq)) hit.append(svg('circle',{cx:x,cy:y,r:7,class:'dot'}));
      attachActivation(hit,()=>selectSquare(sq));board.append(hit);continue;
    }
    const red=isRed(p),checked=!branchPreview && game?.inCheck && ((game.turn==='red'&&p==='K')||(game.turn==='black'&&p==='k'));
    const group=svg('g',{class:`piece ${red?'red-piece':''} ${selected===sq?'selected':''} ${checked?'in-check':''}`,role:'button',tabindex:0,'aria-label':`${red?'红':'黑'}${names[p]} ${sq}${selected===sq?'，已选中':''}`, 'aria-pressed':String(selected===sq)});
    group.append(svg('circle',{cx:x,cy:y,r:23,class:'face'}),svg('circle',{cx:x,cy:y,r:19.5,class:'ring'}),svg('text',{x,y:y+.5},names[p]||p));
    if(targets.includes(sq))group.append(svg('circle',{cx:x,cy:y,r:25,class:'capture-target'}));
    attachActivation(group,()=>selectSquare(sq));board.append(group);
  }
}

function isSelfplay(){return game?.mode==='selfplay'||isDuel();}
function sideLabel(side){return side==='red'?'红方':'黑方';}
function updateControls(){
  for(const id of ['restart','mode-standard','mode-selfplay','mode-duel','play-again','think-time','decision-model','opponent-model','open-puzzles','choose-position'])$(id).disabled=busy||!game;
  $('opponent-model-label').hidden=!isDuel();
  $('undo').disabled=busy||autoPlaying||!game?.moves?.length;
  $('retry').hidden=!(game&&isPlaying()&&!isSelfplay()&&game.turn==='black'&&!busy);
  $('selfplay-controls').hidden=!isSelfplay();
  $('swap-sides').hidden=!isDuel();
  $('swap-sides').disabled=busy||autoPlaying||!game;
  $('auto-play').disabled=!game||!isPlaying()||(busy&&!autoPlaying);
  $('auto-play').textContent=autoPlaying?'Ⅱ 暂停观战':'▶ '+((game?.moves?.length||0)?'继续自对弈':'开始自对弈');
  $('single-step').disabled=busy||autoPlaying||!game||!isPlaying();
  $('auto-status').textContent=!isPlaying()?'对局已结束':autoPlaying?'双方轮流独立决策':busy?'本步结束后暂停':'已暂停 · 可单步观察';
  $('undo').querySelector('span').textContent=isSelfplay()?'撤回一步':'悔棋';
  document.querySelectorAll('.puzzle-card').forEach(el=>el.disabled=busy);
}
function setBusy(value, thinking=false) {
  busy=value;updateControls();
  $('search-progress').hidden=!thinking;
  if(timer){clearInterval(timer);timer=null;}
  if(thinking){
    const started=performance.now();
    $('thinking-title').textContent=providerName(sideModel(game.turn))+' · '+sideLabel(game.turn)+'正在推演';
    $('thinking-elapsed').textContent='0.0s';
    timer=setInterval(()=>{
      const sec=(performance.now()-started)/1000;
      $('thinking-elapsed').textContent=sec.toFixed(1)+'s';
      $('thinking-detail').textContent=sec>Number($('think-time').value)+1?'正在等待模型决策与合法性校验…':'搜索候选着法，校验战术…';
    },100);
  }
  updatePlayers();
}
function stopAuto(note){
  autoPlaying=false;autoEpoch++;clearTimeout(autoTimer);autoTimer=null;
  updateControls();updatePlayers();
  if(note)message(note);
}
async function autoTick(epoch){
  if(epoch!==autoEpoch||!autoPlaying||busy||!isSelfplay()||!isPlaying())return;
  if(document.hidden){stopAuto('页面已切到后台，自对弈暂停。返回后可继续。');return;}
  const ok=await think();
  if(ok&&autoPlaying&&epoch===autoEpoch&&isPlaying())autoTimer=setTimeout(()=>autoTick(epoch),0);
  else if(!isPlaying()){stopAuto();}
}
function startAuto(){
  if(busy||!isSelfplay()||!isPlaying())return;
  clearBranchPreview();autoPlaying=true;const epoch=++autoEpoch;
  updateControls();autoTick(epoch);
}
function updatePlayers() {
  if(!game)return;
  const red=game.turn==='red',self=isSelfplay(),redName=providerUpper(sideModel('red')),blackName=providerUpper(sideModel('black'));
  $('red-name').textContent=self?redName+' · 红方':'你 · 红方';
  $('red-avatar').textContent=self?redName[0]:'帅';
  $('black-name').textContent=blackName+' · 黑方';
  $('black-avatar').textContent=blackName[0];
  for(const side of ['red','black']){
    const current=game.turn===side,el=$(side+'-status');
    el.classList.toggle('active',current&&isPlaying());
    el.textContent=!isPlaying()?'对局结束':current?(busy?'推演中…':game.inCheck?'被将军 · 待应手':self||side==='black'?'待决策':'轮到你了'):'等待对手';
  }
  $('player-detail').textContent=self?'独立红方决策 · 每步 '+$('think-time').value+' 秒':!isPlaying()?'每一局，都是新的起点':red?'点击红方棋子，再点击绿色落点':'你的着法已记录，等待 '+providerName(sideModel('black'))+' 回应';
  $('opponent-detail').textContent=(self?'独立黑方决策':'全力搜索')+' · 每步 '+$('think-time').value+' 秒';
}
function renderGame(data) {
  game=data.game||data;selected=null;branchPreview=null;storeGame(game.id);
  const standard=!game.puzzle||game.puzzle.id==='standard',redName=providerName(sideModel('red')),blackName=providerName(sideModel('black'));
  $('decision-model').value=selectedModel();
  $('opponent-model').value=sideModel('black');
  $('decision-model-label').firstChild.textContent=isDuel()?'红方模型':'决策模型';
  $('game-title').textContent=standard?'标准对局':game.puzzle.title;
  $('game-tag').textContent=isSelfplay()?redName+' × '+blackName:standard?'红先黑后':'残局挑战';
  $('mode-standard').classList.toggle('active',game.mode==='human');
  $('mode-selfplay').classList.toggle('active',game.mode==='selfplay');
  $('mode-duel').classList.toggle('active',isDuel());
  $('match-mode').textContent=isDuel()?redName+' × '+blackName+' 观战':isSelfplay()?'双 '+redName+' 观战':'人机对弈';
  $('mode-description').textContent=isDuel()?'红方 '+redName+'、黑方 '+blackName+' 分别决策；逐步对照两种模型的原选与实战结果。':isSelfplay()?'红黑双方分别调用 '+redName+'。对照原始选择、守门干预与实战结果。':'执红挑战 '+blackName+'，逐步对照模型原选与实际落子。';
  $('model-credit').textContent=(isDuel()?redName+' / '+blackName:blackName)+' 选招 · Pikafish 评估与守门';
  $('model-detail').textContent=isDuel()?redName.toUpperCase()+' × '+blackName.toUpperCase():selectedModel()==='jev'?'JEV · 远程':selectedModel()==='kev'?'KEV · 本地': 'LAYA'+(serviceHealth?.laya?.device?' · '+String(serviceHealth.laya.device).toUpperCase():'');
  const ready=serviceHealth?.engine?.status==='ready'&&serviceHealth?.[sideModel('red')]?.status==='ready'&&serviceHealth?.[sideModel('black')]?.status==='ready';
  $('connection-dot').className=ready?'online':'error';
  $('connection-text').textContent=ready?(isDuel()?'双模型已配置':selectedModel()==='jev'?'Jev 已配置':redName+' 已就绪'):'模型待就绪';
  $('position-note').textContent=standard?'标准开局 · 棋盘与决策同步记录':game.puzzle.subtitle;
  $('log-count').textContent=String(allLogs().length).padStart(2,'0');
  $('result-overlay').hidden=isPlaying();
  const result={
    red_win:[isSelfplay()?'红方 '+redName+' 胜。':'你，胜出。','红方获胜。'],
    black_win:[isSelfplay()?'黑方 '+blackName+' 胜。':'此局，'+blackName+' 胜。','黑方获胜。'],
    draw:['此局，和棋。','双方未能分出胜负。']
  }[game.status];
  if(result){$('result-title').textContent=result[0];$('result-detail').textContent=game.reason||result[1];}
  const ruling=game.adjudication;
  $('adjudication-note').hidden=!ruling;
  $('adjudication-note').classList.toggle('violation',ruling?.kind==='perpetual_check');
  $('adjudication-note').textContent=ruling?(game.reason||ruling.reason):'';
  if(!isPlaying()){autoPlaying=false;clearTimeout(autoTimer);autoTimer=null;}
  $('return-live').hidden=true;
  renderBoard();renderLogs();renderTree();renderScoreboard();if($('puzzle-dialog').open)renderPuzzles();updatePlayers();updateControls();
}
async function newGame(puzzle='standard',mode=game?.mode||'human',model=selectedModel(),opponentModel=game?.opponentModel) {
  if(busy)return;
  stopAuto();setBusy(true);message('正在布置棋局…');
  try{
    treeIndex=-1;decisionSide='all';$('decision-side').value='all';
    const body={puzzle,mode,model};
    if(mode==='duel')body.opponentModel=opponentModel&&opponentModel!==model?opponentModel:oppositeModel(model);
    renderGame(await api('/api/game',body));
    if(isSelfplay())changeFilter('tree');
    message(isSelfplay()?'双方已就位。点击「开始自对弈」，或用「单步决策」逐手观察。':puzzle==='standard'?'你执红先行。选中棋子，开始对弈。':game.puzzle?.subtitle||'红方先行，寻找破局之路。');
  }catch(error){if(game)$('decision-model').value=selectedModel();message(error.message,true);}
  finally{setBusy(false);}
}
async function selectSquare(sq) {
  if(busy||isSelfplay()||branchPreview||!isPlaying()||game.turn!=='red')return;
  const [r,c]=coords(sq),p=boardState()[r][c];
  if(selected&&legalMoves().includes(selected+sq)){await humanMove(selected+sq);return;}
  if(p&&isRed(p)){
    selected=selected===sq?null:sq;
    const count=selected?legalMoves().filter(m=>m.startsWith(sq)).length:0;
    message(selected?(count?names[p]+'已选中 · '+count+' 个合法落点':'该棋子当前没有合法走法，请选择其他棋子。'):'已取消选择。');
    renderBoard();
  }else if(selected)message('此步不合法，请选择绿色落点；不能使己方帅暴露在将军中。');
}
async function humanMove(move) {
  setBusy(true);message('正在校验着法…');
  try{
    renderGame(await api('/api/move',{gameId:game.id,move,version:game.version}));
    if(isPlaying()&&game.turn==='black')await think();
    else message('对局结束，可重新开局或选择其他残局。');
  }catch(error){
    if(error.details?.game){renderGame(error.details.game);changeFilter('all');$('logs').scrollTop=0;}
    message(error.message,true);
  }
  finally{setBusy(false);}
}
async function think() {
  if(!game||!isPlaying()||(!isSelfplay()&&game.turn!=='black'))return false;
  clearBranchPreview();setBusy(true,true);message(providerName(sideModel(game.turn))+' · '+sideLabel(game.turn)+'正在搜索进攻线路…');
  try{
    renderGame(await api('/api/think',{gameId:game.id,seconds:Number($('think-time').value),version:game.version}));
    message(isPlaying()?(isSelfplay()?'本步已记录 · 接下来由 '+providerName(sideModel(game.turn))+' · '+sideLabel(game.turn)+' 决策':game.inCheck?providerName(sideModel('black'))+' 将军！请先解除将军。':providerName(sideModel('black'))+' 已落子，轮到你了。'):'对局结束。可查看双方决策路径，或选择其他残局。');
    return true;
  }catch(error){
    stopAuto();
    message(error.message+' 错误详情已记录在侧栏，当前棋局已保留。',true);
    if(error.details?.game)renderGame(error.details.game);
    else{try{renderGame(await api('/api/game?id='+encodeURIComponent(game.id)));}catch{}}
    changeFilter('all');$('logs').scrollTop=0;
    return false;
  }finally{setBusy(false);}
}
function scoreText(score) {
  if(score===undefined||score===null)return '未提供评分';
  if(typeof score==='object') {
    const mate=score.mate??(score.type==='mate'?score.value:null);
    if(mate!==undefined&&mate!==null)return `${Number(mate)>=0?'杀棋':'被杀'} ${Math.abs(Number(mate))} 步`;
    const cp=score.cp??score.value;
    if(cp!==undefined)return `${Number(cp)>=0?'+':''}${(Number(cp)/100).toFixed(2)}`;
    return JSON.stringify(score);
  }
  return `${Number(score)>=0?'+':''}${(Number(score)/100).toFixed(2)}`;
}
function isModelLog(log){return supportedModels.includes(log.kind)&&Boolean(log.move);}
function actorLabel(log){return supportedModels.includes(log.kind)?providerUpper(logModel(log))+' · '+sideLabel(log.side):log.kind==='system'?(log.agent||'棋局记录'):'你 · '+sideLabel(log.side);}
function decisionEntries(){return allLogs().filter(isModelLog).filter(log=>decisionSide==='all'||log.side===decisionSide);}
function rawDetails(label,value) {
  const details=element('details');details.append(element('summary','',label));
  details.addEventListener('toggle',()=>{if(details.open&&!details.querySelector('pre'))details.append(element('pre','',JSON.stringify(value,null,2)));});
  return details;
}
function renderScoreboard(){
  const box=$('decision-scoreboard');box.replaceChildren();
  const logs=(game?.logs||[]).filter(log=>isModelLog(log)&&!log.undone);
  for(const side of ['red','black']){
    const entries=logs.filter(log=>log.side===side);
    const compared=entries.filter(log=>(log.candidates?.length||0)>1);
    const adopted=compared.filter(log=>!log.guarded).length;
    const exact=compared.filter(log=>modelChoice(log)===log.engineBestMove).length;
    const guard=compared.filter(log=>log.guarded).length;
    const card=element('div','side-score '+side);
    card.append(element('span','side-score-name',sideLabel(side)+(side==='red'&&!isSelfplay()?' · 你':' · '+providerUpper(sideModel(side)))));
    const stat=element('strong','',compared.length?adopted+'/'+compared.length:'—');
    card.append(stat,element('span','score-explanation',side==='red'&&!isSelfplay()?'人类执棋':'非强制原选采纳'));
    card.append(element('small','',entries.length?'原选首选 '+exact+'/'+compared.length+' · 守门 '+guard+' 次 · 唯一应手 '+(entries.length-compared.length)+' 次':'等待首次决策'));
    box.append(card);
  }
  const chart=$('evaluation-chart');chart.replaceChildren();
  chart.append(svg('line',{x1:0,y1:21,x2:280,y2:21,stroke:'#ced4c1','stroke-dasharray':'3 4'}));
  const scored=logs.filter(log=>log.score&&((log.score.cp??(log.score.type==='cp'?log.score.value:null))!==null||log.score.mate!==undefined||log.score.type==='mate')).slice(-30);
  const values=scored.map(log=>{
    const score=log.score,cp=score.cp??(score.type==='cp'?score.value:null);
    const value=cp!==null?Number(cp):Math.sign(score.mate??score.value)*1000;
    return (log.side==='black'?-1:1)*value;
  });
  if(values.length){
    const points=values.map((v,i)=>[(i*272/Math.max(values.length-1,1))+4,21-Math.max(-600,Math.min(600,v))/600*17]);
    chart.append(svg('polyline',{points:points.map(p=>p.join(',')).join(' '),fill:'none',stroke:'#9b3e32','stroke-width':1.8}));
    const end=points.at(-1);chart.append(svg('circle',{cx:end[0],cy:end[1],r:2.5,fill:'#9b3e32'}));
    const last=scored.at(-1),score=last.score;
    if(score.mate!==undefined||score.type==='mate')$('evaluation-value').textContent=(values.at(-1)>0?'红':'黑')+'方杀棋';
    else $('evaluation-value').textContent=(values.at(-1)>=0?'+':'')+(values.at(-1)/100).toFixed(2);
  }else $('evaluation-value').textContent='—';
}
function renderLogs() {
  $('logs').hidden=filter==='tree';$('decision-tree').hidden=filter!=='tree';
  if(filter==='tree')return;
  const visible=allLogs().filter(log=>(filter!=='laya'||isModelLog(log)||Boolean(log.raw?.error))&&(decisionSide==='all'||log.side===decisionSide||log.kind==='system'));
  $('logs').replaceChildren();
  if(!visible.length){$('logs').innerHTML=initialEmpty;return;}
  [...visible].reverse().forEach(log=>{
    const ai=isModelLog(log),index=allLogs().indexOf(log)+1;
    const article=element('article','log-entry '+(log.side==='red'?'human':'laya')+(log.undone?' undone':''));
    const top=element('div','log-entry-top');
    top.append(element('span','log-author',actorLabel(log)),element('span','log-number','# '+String(index).padStart(2,'0')));
    article.append(top,element('div','log-move',log.label||log.move||'棋局事件'));
    if(log.undone)article.append(element('span','undone-badge','已撤回 · 留存记录'));
    if(ai){
      const choice=modelChoice(log),original=(log.candidates||[]).find(c=>c.move===choice);
      article.append(element('p','choice-line','模型原选 '+(original?.label||choice||'未解析')+(log.guarded?' → 守门改写':' · 直接采纳')));
      const metrics=element('div','log-metrics');
      if(log.depth)metrics.append(element('span','','深度 '+log.depth));
      if(log.score!==undefined)metrics.append(element('span','',sideLabel(log.side)+'评估 '+scoreText(log.score)));
      if(log.elapsedMs!==undefined)metrics.append(element('span','',(log.elapsedMs/1000).toFixed(2)+' 秒'));
      if(log.timings)metrics.append(element('span','','搜索 '+log.timings.searchMs+'ms / 模型 '+(log.timings.modelMs??log.timings.layaMs)+'ms'));
      if(log.modelRank||log.layaRank)metrics.append(element('span','','原选排名 #'+(log.modelRank||log.layaRank)));
      if(log.guarded)metrics.append(element('span','','守门干预'));
      if(log.repetitionAvoided)metrics.append(element('span','','等优变招 · 打破循环'));
      if(log.historyPlyCount!==undefined)metrics.append(element('span','','已读取 '+log.historyPlyCount+' 步历史'));
      article.append(metrics);
      const treeButton=element('button','tree-link','展开这步决策路径 ↗');
      treeButton.addEventListener('click',()=>{
        decisionSide='all';$('decision-side').value='all';
        treeIndex=decisionEntries().indexOf(log);changeFilter('tree');
      });article.append(treeButton);
      if(log.decisionCriteria)article.append(rawDetails('本步给 '+providerName(logModel(log))+' 的决策因子 · '+(log.decisionFactorVersion||'当前版'),log.decisionCriteria));
      if(log.raw)article.append(rawDetails(providerName(logModel(log))+' 原始输出',modelResponse(log)));
    }else if(log.summary)article.append(element('p','',log.summary));
    if(!ai&&log.raw){
      if(log.raw.error)article.append(element('p','error-reason',readableFailure(log.raw.error)));
      const detailLabel=log.raw.adjudication?.kind==='no_progress'?'裁决依据 · 无进展计数':log.raw.adjudication?'裁决依据 · 循环与将军记录':'错误详情 · 原始响应';
      const details=rawDetails(detailLabel,log.raw);details.open=true;article.append(details);
    }
    $('logs').append(article);
  });
}
function treeNode(className,kicker,title,detail) {
  const node=element('div',`tree-node ${className}`);node.append(element('span','tree-kicker',kicker),element('strong','',title));if(detail)node.append(element('p','',detail));return node;
}
function clearBranchPreview(){
  branchPreview=null;$('return-live').hidden=true;
  if(game)renderBoard();
}
function previewBranch(log,candidate,ply){
  if(busy){message('本步正在推理，请暂停并等待本步结束后预览分支。');return;}
  stopAuto();selected=null;
  const board=parseBoard(log.fenBefore||log.positionBefore);
  const pv=candidate.pv||[candidate.move];
  for(const move of pv.slice(0,ply+1)){
    const [r,c]=coords(move.slice(0,2)),[nr,nc]=coords(move.slice(2));
    if(!board[r]?.[c]){message('这条后续变化无法完整重放，请核对原始搜索输出。',true);return;}
    board[nr][nc]=board[r][c];board[r][c]=null;
  }
  branchPreview={board,move:pv[ply],ply,side:log.side};
  $('return-live').hidden=false;$('return-live').textContent='↩ 返回实战 · 预览第 '+(ply+1)+' 步';
  renderBoard();message('分支预览：'+actorLabel(log)+' · '+(candidate.label||candidate.move)+' · 第 '+(ply+1)+' 步。实际棋局未改变。');
}
function renderTree() {
  const panel=$('decision-tree');panel.replaceChildren();
  const entries=decisionEntries();
  if(!entries.length){
    const empty=element('div','empty-log');
    empty.append(element('div','empty-board','枝'),element('h3','','落子之前，先看分歧。'),element('p','',isSelfplay()?'点击「开始自对弈」或「单步决策」，查看两方各自的候选路径与原始选择。':'模型落子后，这里展示真实候选分支、模型原选与最终执行。'));
    panel.append(empty);return;
  }
  const index=treeIndex<0?entries.length-1:Math.min(treeIndex,entries.length-1),log=entries[index],side=sideLabel(log.side);
  const bar=element('div','tree-selector'),label=element('label','','逐手复盘');label.htmlFor='tree-step';
  const select=element('select');select.id='tree-step';
  entries.forEach((item,i)=>{const option=element('option','','#'+(i+1)+' '+sideLabel(item.side)+' · '+(item.label||item.move)+(item.undone?'（已撤回）':''));option.value=i;option.selected=i===index;select.append(option);});
  select.addEventListener('change',()=>{treeIndex=Number(select.value);clearBranchPreview();renderTree();});
  const follow=element('button','tree-link',treeIndex<0?'● 跟随最新':'跟随最新');
  follow.addEventListener('click',()=>{treeIndex=-1;clearBranchPreview();renderTree();});
  bar.append(label,select,follow);panel.append(bar);
  const candidates=log.candidates||[],choice=modelChoice(log),name=providerName(logModel(log)),original=candidates.find(c=>c.move===choice);
  const comparison=element('div','choice-comparison '+(log.side==='red'?'red-side':'black-side'));
  const before=element('div'),after=element('div');
  before.append(element('span','',providerUpper(logModel(log))+' 原选'),element('strong','',original?.label||choice||'未解析'),element('small','',original?'候选 #'+(candidates.indexOf(original)+1)+' · '+scoreText(original.score):'查看原始响应'));
  after.append(element('span','','实际执行'),element('strong','',log.label||log.move),element('small',log.guarded?'guard-label':'',log.repetitionAvoided?'等优变招 · 打破循环':log.guarded?'经最优守门改写':'模型原选直接采纳'));
  comparison.append(before,element('span','comparison-arrow','→'),after);panel.append(comparison);
  panel.append(element('p','tree-description',side+'视角评分 · '+(log.elapsedMs/1000).toFixed(2)+' 秒 · '+(log.undone?'此步已撤回。':'')+'点击分支中的着法，可在棋盘上预览后续变化。'));
  if(log.timings)panel.append(element('p','tree-description','耗时拆分：搜索 '+log.timings.searchMs+'ms · '+name+' '+(log.timings.modelMs??log.timings.layaMs)+'ms'));
  const tree=element('div','tree');
  tree.append(treeNode('root','01 · '+side.toUpperCase()+'决策',log.legalMoveCount+' 个合法着法 → '+candidates.length+' 个候选','Pikafish 搜索深度 '+(log.depth||'—')+'。评分越高越有利于'+side+'。'));
  const branches=element('div','tree-branches');
  candidates.forEach((candidate,i)=>{
    const chosen=candidate.move===(log.finalMove||log.move),originalChoice=candidate.move===choice;
    const branch=element('div','tree-branch '+(chosen?'chosen ':'')+(originalChoice?'model-chosen':''));
    const bound=candidate.bound==='lowerbound'?'≥ ':candidate.bound==='upperbound'?'≤ ':'';
    const head=element('div','branch-heading');head.append(element('span','','候选 '+String(i+1).padStart(2,'0')+' · 深度 '+candidate.depth),element('span','branch-score',bound+scoreText(candidate.score)));
    branch.append(head,element('strong','branch-move',candidate.label||candidate.move));
    if(originalChoice)branch.append(element('span','branch-status model-badge',name+' 原选'));
    if(chosen)branch.append(element('span','branch-status chosen','实际执行'));
    if(originalChoice&&!chosen)branch.append(element('span','branch-status','守门未采纳'));
    if(candidate.repetitionCount>0)branch.append(element('span','branch-status','目标局面已出现 '+candidate.repetitionCount+' 次'));
    const pv=Array.isArray(candidate.pv)?candidate.pv:typeof candidate.pv==='string'?candidate.pv.split(' '):[];
    if(pv.length){
      const details=element('details','pv-details');details.append(element('summary','','展开后续路径 · '+pv.length+' 步'));
      const line=element('div','pv-line');
      pv.forEach((move,ply)=>{
        const plySide=ply%2===0?log.side:log.side==='red'?'black':'red';
        const button=element('button',plySide==='red'?'pv-red':'pv-black',(ply+1)+'. '+move);
        button.title=sideLabel(plySide)+' · 点击在棋盘上预览';button.addEventListener('click',()=>previewBranch(log,candidate,ply));line.append(button);
      });details.append(line);branch.append(details);
    }
    branches.append(branch);
  });
  tree.append(branches);
  tree.append(treeNode(log.guarded?'guarded':'','02 · 最优评分守门',log.repetitionAvoided?'等优变招，避免重复':log.guarded?'保留引擎最优着':'采纳模型原选',log.guardReason||(log.guarded?'模型原选未通过等优校验。':'模型原选符合当前最优评分约束。')));
  tree.append(element('div','tree-edge','↓'));
  tree.append(treeNode('final','03 · '+side+'最终落子',log.label||log.move,side+'评估 '+scoreText(log.score)));
  if(log.decisionCriteria)tree.append(rawDetails('本步给 '+name+' 的决策因子 · '+(log.decisionFactorVersion||'当前版'),log.decisionCriteria));
  if(log.raw)tree.append(rawDetails('查看 '+name+' 原始响应',modelResponse(log)));
  if(log.fenBefore)tree.append(rawDetails('查看决策前 FEN',log.fenBefore));
  tree.append(element('p','tree-description','这些路径来自实际引擎搜索与 '+name+' 输出，不代表模型内部思维或完整博弈树。'));
  panel.append(tree);
}
function renderPuzzles() {
  const list=$('puzzle-list');list.replaceChildren();
  document.querySelectorAll('.difficulty-filter').forEach(button=>{button.hidden=button.dataset.difficulty!=='all'&&!puzzles.some(p=>p.difficulty===button.dataset.difficulty);});
  const shown=puzzles.filter(p=>difficulty==='all'||p.difficulty===difficulty);
  if(!chosenPuzzle)chosenPuzzle=puzzles.find(p=>p.id===game?.puzzle?.id)||shown[0]||puzzles[0];
  shown.forEach(puzzle=>{
    const index=puzzles.indexOf(puzzle),card=element('button','puzzle-card '+(chosenPuzzle?.id===puzzle.id?'active':''));
    card.disabled=busy;card.setAttribute('aria-label','预览残局 '+(index+1)+'：'+puzzle.title);
    const top=element('div','puzzle-top');top.append(element('span','puzzle-index',String(index+1).padStart(2,'0')),element('span','puzzle-difficulty',puzzle.difficulty));
    card.append(top,element('strong','',puzzle.title),element('small','',puzzle.subtitle));
    card.addEventListener('click',()=>{chosenPuzzle=puzzle;renderPuzzles();});
    list.append(card);
  });
  if(!shown.length)list.append(element('p','loading-text','该分级暂无残局，请查看其他分级。'));
  renderPuzzlePreview();
}
function renderPuzzlePreview(){
  const p=chosenPuzzle;if(!p)return;
  $('preview-title').textContent=p.title;
  $('preview-index').textContent='POSITION / '+String(puzzles.indexOf(p)+1).padStart(2,'0');
  $('preview-description').textContent=p.subtitle;
  const board=parseBoard(p.fen),canvas=$('preview-board');canvas.replaceChildren();
  for(let r=0;r<10;r++)canvas.append(svg('line',{x1:27,y1:24+r*27.5,x2:243,y2:24+r*27.5,stroke:'#c1b28f','stroke-width':.6}));
  for(let c=0;c<9;c++)canvas.append(svg('line',{x1:27+c*27,y1:24,x2:27+c*27,y2:271.5,stroke:'#c1b28f','stroke-width':.6}));
  for(const y of [24,216.5]){canvas.append(svg('path',{d:'M108 '+y+' L162 '+(y+55)+' M162 '+y+' L108 '+(y+55),stroke:'#c1b28f',fill:'none','stroke-width':.6}));}
  canvas.append(svg('rect',{x:28,y:135,width:214,height:26,fill:'#efe4cc'}),svg('text',{x:135,y:152,'text-anchor':'middle',fill:'#9a8e71','font-size':12},'楚 河       汉 界'));
  let count=0;
  board.forEach((row,r)=>row.forEach((piece,c)=>{
    if(!piece)return;count++;
    const x=27+c*27,y=24+r*27.5;
    canvas.append(svg('circle',{cx:x,cy:y,r:11.5,fill:'#fff4d9',stroke:'#b4a07d','stroke-width':.7}),svg('text',{x,y:y+4.5,'text-anchor':'middle','font-size':14,'font-family':'Songti SC, serif',fill:isRed(piece)?'#9b3e32':'#354033'},names[piece]));
  }));
  $('preview-metrics').replaceChildren(element('span','',count+' 枚棋子'),element('span','',p.difficulty),element('span','',p.theme||'实战型残局'));
  if(p.analysis?.legalMoveCount)$('preview-metrics').append(element('span','',p.analysis.legalMoveCount+' 个首步分支'));
  $('enter-puzzle').disabled=busy;
}
function openPuzzleDialog(){
  if(busy)return;stopAuto();
  const name=providerName(selectedModel());
  const duelRed=isDuel()?providerName(sideModel('red')):'Jev',duelBlack=isDuel()?providerName(sideModel('black')):'Laya';
  $('puzzle-mode').options[0].textContent=name+' × '+name+' · 观战';
  $('puzzle-mode').options[1].textContent=duelRed+' 红 × '+duelBlack+' 黑 · 对弈';
  $('puzzle-mode').options[2].textContent='我执红 · 挑战 '+name;
  $('enter-puzzle').textContent='进入此残局 ↗';
  $('puzzle-mode').value=game?.mode||'human';
  renderPuzzles();$('puzzle-dialog').showModal();
}
function changeFilter(value){filter=value;document.querySelectorAll('.log-filter').forEach(el=>el.classList.toggle('active',el.dataset.filter===value));renderLogs();renderTree();}
document.querySelectorAll('.log-filter').forEach(el=>el.addEventListener('click',()=>changeFilter(el.dataset.filter)));
$('decision-side').addEventListener('change',()=>{decisionSide=$('decision-side').value;treeIndex=-1;renderLogs();renderTree();});
$('restart').addEventListener('click',()=>newGame(game?.puzzle?.id||'standard'));
$('play-again').addEventListener('click',()=>newGame(game?.puzzle?.id||'standard'));
$('review-result').addEventListener('click',()=>{$('result-overlay').hidden=true;changeFilter('all');$('logs').scrollTop=0;message(game.reason||'对局已结束，可查看棋局和全部决策记录。');});
$('mode-standard').addEventListener('click',()=>newGame(game?.puzzle?.id||'standard','human'));
$('mode-selfplay').addEventListener('click',()=>newGame(game?.puzzle?.id||'standard','selfplay'));
$('mode-duel').addEventListener('click',()=>newGame(game?.puzzle?.id||'standard','duel','jev','laya'));
$('decision-model').addEventListener('change',()=>{
  const model=$('decision-model').value;
  const opponent=isDuel()&&$('opponent-model').value!==model?$('opponent-model').value:isDuel()?oppositeModel(model):undefined;
  newGame(game?.puzzle?.id||'standard',game?.mode||'human',model,opponent);
});
$('opponent-model').addEventListener('change',()=>{
  if(!isDuel())return;
  const opponent=$('opponent-model').value,red=selectedModel();
  newGame(game?.puzzle?.id||'standard','duel',red,opponent===red?oppositeModel(red):opponent);
});
$('swap-sides').addEventListener('click',()=>{if(isDuel()&&!busy&&!autoPlaying)newGame(game?.puzzle?.id||'standard','duel',sideModel('black'),sideModel('red'));});
$('auto-play').addEventListener('click',()=>{if(autoPlaying)stopAuto(busy?'本步完成后暂停，决策记录会保留。':'自对弈已暂停。');else startAuto();});
$('single-step').addEventListener('click',()=>{if(!busy&&!autoPlaying)think();});
$('return-live').addEventListener('click',()=>{clearBranchPreview();message('已返回实际棋局。');});
$('open-puzzles').addEventListener('click',openPuzzleDialog);
$('choose-position').addEventListener('click',openPuzzleDialog);
$('close-puzzles').addEventListener('click',()=>$('puzzle-dialog').close());
$('enter-puzzle').addEventListener('click',async()=>{if(!chosenPuzzle||busy)return;const mode=$('puzzle-mode').value;$('puzzle-dialog').close();await newGame(chosenPuzzle.id,mode,mode==='duel'?(isDuel()?sideModel('red'):'jev'):selectedModel(),mode==='duel'?(isDuel()?sideModel('black'):'laya'):undefined);});
$('standard-position').addEventListener('click',async()=>{const mode=$('puzzle-mode').value;$('puzzle-dialog').close();await newGame('standard',mode,mode==='duel'?(isDuel()?sideModel('red'):'jev'):selectedModel(),mode==='duel'?(isDuel()?sideModel('black'):'laya'):undefined);});
document.querySelectorAll('.difficulty-filter').forEach(button=>button.addEventListener('click',()=>{
  difficulty=button.dataset.difficulty;
  document.querySelectorAll('.difficulty-filter').forEach(el=>el.classList.toggle('active',el===button));
  if(difficulty!=='all'&&chosenPuzzle?.difficulty!==difficulty)chosenPuzzle=puzzles.find(p=>p.difficulty===difficulty)||chosenPuzzle;
  renderPuzzles();
}));
$('flip').addEventListener('click',()=>{flipped=!flipped;renderBoard();});
$('retry').addEventListener('click',()=>{if(!busy)think();});
$('think-time').addEventListener('change',()=>{
  updatePlayers();try{localStorage.setItem('laya-xiangqi-search-seconds-fast',$('think-time').value);}catch{}
});
$('undo').addEventListener('click',async()=>{
  if(busy||!game?.moves?.length)return;stopAuto();setBusy(true);
  try{
    renderGame(await api('/api/undo',{gameId:game.id,version:game.version}));
    treeIndex=-1;renderTree();message(isSelfplay()?'已撤回一步，原决策仍保留供复盘。':'已撤回上一回合，重新思考你的着法。');
  }catch(error){
    if(error.details?.game){renderGame(error.details.game);changeFilter('all');$('logs').scrollTop=0;}
    message(error.message,true);
  }finally{setBusy(false);}
});
$('export').addEventListener('click',()=>{
  if(!game){message('棋局尚未就绪。',true);return;}
  const blob=new Blob([JSON.stringify({exportedAt:new Date().toISOString(),game,notice:'红黑双方使用本局选择的决策模型。日志区分模型原选和引擎守门后的实际落子，不代表模型独立棋力或内部思维。'},null,2)],{type:'application/json'});
  const url=URL.createObjectURL(blob),a=element('a');a.href=url;a.download='laya-xiangqi-'+game.id+'.json';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);message('完整棋局、双方原选与决策分支已导出。');
});
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&selected){selected=null;renderBoard();message('已取消选择。');}});
document.addEventListener('visibilitychange',()=>{if(document.hidden&&autoPlaying)stopAuto('页面进入后台，自对弈已暂停。');});
window.addEventListener('pagehide',()=>stopAuto());
async function init(){
  restoreSearchTime();setBusy(true);renderBoard();
  try{
    const [health,collection]=await Promise.all([api('/api/health'),api('/api/puzzles')]);
    serviceHealth=health;
    puzzles=collection.puzzles||collection;renderPuzzles();
    const saved=storedGame();
    if(saved){try{renderGame(await api('/api/game?id='+encodeURIComponent(saved)));}catch{game=null;}}
    if(!game)renderGame(await api('/api/game',{puzzle:'standard',mode:'human',model:'laya'}));
    changeFilter('tree');
    message(isSelfplay()?'棋局已恢复。自对弈处于暂停状态，点击继续或单步决策。':isPlaying()?(game.turn==='black'?'棋局已恢复，点击「重试模型决策」继续。':game.inCheck?'当前红方被将军，请先解除将军。':'你执红先行；也可切换「模型自对弈」观察双方决策。'):'已恢复结束的棋局，可重新开始。');
    if(!storageAvailable)message('浏览器存储不可用，刷新后将新建棋局。');
  }catch(error){
    $('connection-dot').className='error';$('connection-text').textContent='连接失败';message('连接失败：'+error.message+'。请确认本地服务已启动，再刷新页面。',true);
  }finally{setBusy(false);}
}
init();
