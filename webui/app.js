(() => {
  const root = document.documentElement;
  const systemTheme = matchMedia('(prefers-color-scheme: dark)');
  let preference = null;
  try { const saved = localStorage.getItem('helper-theme'); if (saved === 'light' || saved === 'dark') preference = saved; } catch {}
  function applyTheme(theme) {
    root.dataset.theme = theme;
    const button = document.querySelector('#theme-toggle');
    if (!button) return;
    const target = theme === 'dark' ? '亮色' : '暗色';
    button.querySelector('span').textContent = '切换到' + target;
    button.setAttribute('aria-label', '切换到' + target); button.title = '切换到' + target;
  }
  const preferredTheme = () => preference || (systemTheme.matches ? 'dark' : 'light');
  applyTheme(preferredTheme());
  systemTheme.addEventListener('change', () => { if (!preference) applyTheme(preferredTheme()); });
  window.addEventListener('storage', event => {
    if (event.key !== 'helper-theme' && event.key !== null) return;
    preference = event.newValue === 'light' || event.newValue === 'dark' ? event.newValue : null;
    applyTheme(preferredTheme());
  });
  document.addEventListener('DOMContentLoaded', () => {
    applyTheme(preferredTheme());
    document.querySelector('#theme-toggle').onclick = () => {
      preference = root.dataset.theme === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem('helper-theme', preference); } catch {}
      applyTheme(preference);
    };
const $ = (q, root = document) => root.querySelector(q);
const token = $('meta[name="helper-token"]').content;
let state = null, config = null, logCursor = 0, logs = [], paused = false, busy = false;
let selectedPlugin = '', toastTimer, pollBusy = false, serviceAvailable = true;
let robot = sessionStorage.getItem('helper-robot') || 'default', robotRows = [], lastRobotFetch = 0;
let pluginSignature = '', accountSignature = '', robotOptionsSignature = '';
let messages=[], messageCursor=0, messageStream='', messagePaused=false, messageError='', messageGeneration=0, messageSignature='';
let loggedAccounts = [], selectedAccounts = new Set(), accountsError = '', accountsLoading = true, accountsFetching = false, lastAccountFetch = 0, batchStarting = false;
let sendMembers = [], mentionSelection = new Set(), sendSession = '', sendGeneration = 0, sendLoading = false, pendingSend = null, sendPolling = false;
let chatRows=[], chatLoadKey='', chatTimelineSignature='', chatConfirm=null, chatDraftId='';
let chatListMarkup='', chatTimelineTarget='';
const chatDrafts=new Map();
// 开了长期记忆的会话，点开时从库里取以前的聊天，接在本次收到的消息前面。
// historyLoading：会话 → 本次请求的标记；清空记忆时删掉，清空前发出、迟到的旧结果不再写入。
const chatHistory=new Map(),historyLoading=new Map();
async function loadChatHistory(chat){
  const key=robot+':'+chat,requested=robot,token={};
  if(!chat||chatHistory.has(key)||historyLoading.has(key))return;
  historyLoading.set(key,token);
  const live=messages.filter(message=>message.chat_id===chat&&message.timestamp).map(message=>message.timestamp);
  const before=live.length?Math.min(...live):0;  // 只取本次已显示消息之前的，两段接上不重复
  try{
    const {rows}=await api('chat-history?chat='+encodeURIComponent(chat)+(before?'&before='+before:''),undefined,requested);
    if(requested===robot&&historyLoading.get(key)===token){chatHistory.set(key,rows);chatTimelineSignature='';renderChatWorkspace();}
  }catch{}finally{if(historyLoading.get(key)===token)historyLoading.delete(key);}
}
const accountKey = row => row.pid + ':' + row.wxid;
const activeStates = ['starting','running','stopping'];
const profileLabel = row => loggedAccounts.find(account=>account.wxid===row?.account)?.name || row?.account || (row?.id==='default'?'默认配置':row?.name) || '当前账号';
const pages = {chat:'聊天', overview:'账号与监控', plugins:'插件管理', groups:'群聊配置', models:'模型与语音', logs:'运行日志'};
const pluginNames = {agent_chat:['智能体'], anti_recall:['文字防撤回'], anti_recall_image:['图片防撤回'], anti_recall_file:['文件防撤回'], picture_idiom:['看图猜成语'], idiom_chain:['成语接龙'], chat_summary:['聊天总结'], mention_chat:['艾特对话'], voice_reply:['语音回复'], group_notice:['进退群提醒'], member_identity:['群内昵称绑定'], personality:['切换人格'], friend_auto_accept:['自动通过好友'], chat_records:['聊天记录管理'], id_responder:['会话 ID 查询'], echo:['文字回声']};
const escape = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, body, identifier = robot) {
  const scope = (path === 'robots' || path === 'accounts' || path.startsWith('accounts/')) ? '' : (path.includes('?') ? '&' : '?') + 'robot=' + encodeURIComponent(identifier);
  const response = await fetch('/api/' + path + scope, {method: body === undefined ? 'GET' : 'POST', cache:'no-store',
    headers:{'X-Helper-Token':token, ...(body === undefined ? {} : {'Content-Type':'application/json'})},
    ...(body === undefined ? {} : {body:JSON.stringify(body)})});
  const data = await response.json();
  if (!response.ok) { const error=new Error(data.error||'操作失败');error.status=response.status;throw error; }
  return data;
}
function toast(message, error = false) {
  const dialogError = $('dialog[open] .dialog-error');
  if (error && dialogError) { dialogError.textContent = message; dialogError.classList.remove('hidden'); return; }
  const node = $('#toast'); node.textContent = message; node.className = 'toast' + (error ? ' error' : '');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => node.classList.add('hidden'), error ? 7000 : 3500);
}
function openDialog(selector) {
  const dialog = $(selector); $('.dialog-error', dialog).classList.add('hidden');
  $('#toast').classList.add('hidden'); dialog.showModal();
}
async function action(callback) {
  if (!serviceAvailable) { toast('管理服务尚未恢复连接，请稍候或刷新页面', true); return; }
  if (busy) return; busy = true; renderState();
  document.querySelectorAll('.dialog-error').forEach(node=>node.classList.add('hidden'));
  try { await callback(); await poll(); } catch (error) { toast(error.message, true); }
  finally { busy = false; renderState(); }
}
function navigate() {
  const name = location.hash.slice(1) in pages ? location.hash.slice(1) : 'chat';
  document.body.classList.toggle('chat-page',name==='chat');
  document.querySelectorAll('.page').forEach(node => node.classList.toggle('hidden', node.id !== 'page-' + name));
  document.querySelectorAll('[data-page]').forEach(node => node.classList.toggle('active', node.dataset.page === name));
  document.querySelectorAll('[data-page]').forEach(node => node.dataset.page === name ? node.setAttribute('aria-current','page') : node.removeAttribute('aria-current'));
  $('#page-label').textContent = pages[name]; document.title = pages[name] + ' · helper';
  if (document.body.classList.contains('nav-open')) { setNavigation(false); $('#workspace').focus({preventScroll:true}); }
  window.scrollTo(0, 0);
  if(name==='chat')renderChatWorkspace();
}
function renderRobotOptions() {
  const names={stopped:'已停止',starting:'启动中',running:'等待连接',stopping:'停止中',error:'异常'};
  const options=robotRows.map(row=>{
    const current=row.id===robot&&state?state:row;
    return {value:row.id,label:profileLabel(row)+' · '+(!serviceAvailable?'状态待更新':current.connected?'已连接':names[current.state]||'已停止')};
  });
  for(const account of loggedAccounts) {
    if(robotRows.some(row=>row.account===account.wxid))continue;
    options.push({value:'account:'+accountKey(account),label:(account.name||account.wxid)+' · 未启动',disabled:!account.selectable});
  }
  const signature=JSON.stringify(options);
  if(signature!==robotOptionsSignature) {
    robotOptionsSignature=signature;
    $('#robot-select').innerHTML=options.map(row=>`<option value="${escape(row.value)}" ${row.disabled?'disabled':''}>${escape(row.label)}</option>`).join('');
  }
  $('#robot-select').value=robot;
  // 左上角和微信一样显示当前账号的头像。
  const current=robotRows.find(row=>row.id===robot),name=current?profileLabel(current):'';
  $('.brand').innerHTML=name?chatAvatar(name):'';
  $('.brand').title=name||'账号菜单';$('#account-label').textContent=name;
  // 头像菜单：账号列表由隐藏的 #robot-select 生成，切换仍走它的 change 事件。
  const locked=$('#robot-select').disabled,selected=options.find(row=>row.value===robot);
  $('#menu-account-name').textContent=name||'未选择账号';
  $('#menu-account-state').textContent=selected?selected.label.split(' · ').pop():'';
  $('#menu-accounts').innerHTML=options.map(row=>`<button type="button" class="menu-item" role="menuitemradio" aria-checked="${row.value===robot}" data-robot="${escape(row.value)}" ${row.disabled||locked?'disabled':''}><span class="menu-text">${escape(row.label)}</span>${row.value===robot?'<svg class="menu-trail" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>':''}</button>`).join('');
}
function renderState() {
  const unavailable=!serviceAvailable;
  $('#robot-select').disabled=busy||unavailable||!!pendingSend;  // 发送途中不能切换账号
  $('#discover-chats').disabled=busy||unavailable||!state||!activeStates.includes(state.state);
  renderRobotOptions();renderAccounts();renderMessages();
  const running=!!state&&activeStates.includes(state.state);
  document.querySelectorAll('form:not(#send-form) .primary').forEach(node=>node.disabled=unavailable||!state||running||busy);
  renderSendState();
  document.querySelectorAll('.save-hint').forEach(node=>node.textContent=unavailable?'连接中断':!state?'正在读取':running?'停止后可保存':'启动后生效');
  if(state)$('#nav-plugin-count').textContent=state.plugins.length;
  renderPlugins();
  renderChatWorkspace();
}
function renderPlugins() {
  if (!state) { pluginSignature=''; $('#plugin-list').innerHTML='<tr><td colspan="4" class="table-empty">正在读取插件列表…</td></tr>'; return; }
  const query = $('#plugin-search').value.trim().toLowerCase();
  const cards = state.plugins.filter(p => (p.name + (pluginNames[p.name]?.[0]||'') + p.description).toLowerCase().includes(query));
  const signature = JSON.stringify([robot,query,busy,serviceAvailable,cards]);
  if (signature === pluginSignature) return;
  pluginSignature = signature;
  const focused = document.activeElement?.closest('[data-plugin]');
  const focusKey = focused && [focused.dataset.plugin,focused.dataset.action];
  $('#plugin-list').innerHTML = cards.map(p => {
    const name = pluginNames[p.name]?.[0] || p.name;
    return `<tr class="plugin-row"><td><span class="plugin-name">${escape(name)}</span><span class="plugin-code">${escape(p.name)}</span></td><td class="plugin-description">${escape(p.description)}${p.error?`<p class="plugin-error">${escape(p.error)}</p>`:''}</td><td><span class="inline-state ${!serviceAvailable?'waiting':p.active?'active':p.error?'failed':''}">${!serviceAvailable?'待更新':p.active?'运行中':p.enabled?'已启用':'已停用'}</span></td><td><div class="plugin-actions"><button class="secondary small" data-plugin="${escape(p.name)}" data-action="${p.enabled?'disable':'enable'}" ${busy||!serviceAvailable?'disabled':''}>${p.enabled?'停用':'启用'}</button><button class="text-button" data-plugin="${escape(p.name)}" data-action="reload" ${!p.active||busy||!serviceAvailable?'disabled':''}>重载</button><button class="text-button" data-plugin="${escape(p.name)}" data-action="configure" ${busy||!serviceAvailable?'disabled':''}>配置</button></div></td></tr>`;
  }).join('') || '<tr><td colspan="4" class="table-empty">没有匹配的插件。</td></tr>';
  if (focusKey) {
    const candidates = [...$('#plugin-list').querySelectorAll('[data-plugin]')];
    const target = candidates.find(n=>n.dataset.plugin===focusKey[0]&&n.dataset.action===focusKey[1]&&!n.disabled) || candidates.find(n=>n.dataset.plugin===focusKey[0]&&!n.disabled);
    target?.focus({preventScroll:true});
  }
}
function renderAccounts() {
  const rows=loggedAccounts.map(row=>{
    const profile=robotRows.find(profile=>profile.id===row.robot_id);
    return row.robot_id===robot&&state ? {...row,state:state.state,connected:state.connected,error:state.error||state.connection_error} : profile ? {...row,state:profile.state,connected:profile.connected} : row;
  });
  const chosen=rows.filter(row=>selectedAccounts.has(accountKey(row))&&row.selectable&&!activeStates.includes(row.state));
  $('#account-total').textContent=rows.length;
  $('#account-selection-count').textContent='已选 '+chosen.length;
  $('#refresh-accounts').disabled=busy||!serviceAvailable||accountsFetching;
  $('#start-selected').disabled=busy||!serviceAvailable||accountsLoading||!chosen.length;
  $('#start-selected').textContent=batchStarting?'正在启动…':'启动所选';
  $('#account-error').textContent=accountsError;$('#account-error').classList.toggle('hidden',!accountsError);
  const signature=JSON.stringify([rows,[...selectedAccounts],busy,serviceAvailable,accountsLoading]);
  if(signature===accountSignature)return;
  accountSignature=signature;
  const focused=document.activeElement;
  const focusKey=focused?.dataset.selectAccount||focused?.dataset.accountAction;
  const focusSelector=focused?.dataset.selectAccount?'[data-select-account]':'[data-account-action]';
  const statuses={stopped:'未启动',starting:'启动中',running:'等待连接',stopping:'正在停止',error:'异常'};
  $('#account-list').innerHTML=rows.map(row=>{
    const key=accountKey(row),running=activeStates.includes(row.state),disabled=busy||!serviceAvailable||accountsLoading||!row.selectable||running;
    const label=row.name||row.wxid;
    const stopping=row.state==='stopping',canStop=running&&!!row.robot_id;
    const buttonDisabled=busy||!serviceAvailable||stopping||(canStop?false:accountsLoading||!row.selectable);
    const buttonLabel=stopping?'停止中…':canStop?'停止':'启动';
    return `<tr class="${row.robot_id===robot?'selected':''}"><td class="account-choice"><label><input type="checkbox" data-select-account="${escape(key)}" aria-label="选择 ${escape(label)} ${escape(row.wxid)}" ${selectedAccounts.has(key)?'checked':''} ${disabled?'disabled':''}></label></td><td><span class="robot-name">${escape(label)}</span><span class="account-id">${escape(row.wxid)}</span></td><td class="account-pid">${escape(row.pid)}</td><td><span class="inline-state ${!serviceAvailable?'waiting':row.reason?'failed':row.connected?'active':row.state==='error'?'failed':running?'waiting':''}">${escape(!serviceAvailable?'待更新':row.reason|| (row.connected?'已连接':statuses[row.state]||'未启动'))}</span>${row.error?`<p class="account-error">${escape(row.error)}</p>`:''}</td><td class="account-action"><button class="secondary small" data-account-action="${escape(key)}" data-action="${canStop?'stop':'start'}" aria-label="${buttonLabel} ${escape(label)} ${escape(row.wxid)}" ${buttonDisabled?'disabled':''}>${buttonLabel}</button></td></tr>`;
  }).join('')||`<tr><td colspan="5" class="table-empty">${accountsLoading?'正在识别账号…':accountsError?'账号识别失败，请刷新重试。':'未发现已登录微信。'}</td></tr>`;
  if(focusKey){const target=[...$('#account-list').querySelectorAll(focusSelector)].find(node=>(node.dataset.selectAccount||node.dataset.accountAction)===focusKey);if(target&&!target.disabled)target.focus({preventScroll:true});}
}
async function loadAccounts(quiet=false) {
  if(accountsFetching)return;
  accountsFetching=true;accountsLoading=!quiet||!loggedAccounts.length;renderAccounts();
  try{
    const data=await api('accounts');loggedAccounts=data.accounts;accountsError='';
    const available=new Set(loggedAccounts.filter(row=>row.selectable&&!activeStates.includes(row.state)).map(accountKey));
    selectedAccounts=new Set([...selectedAccounts].filter(key=>available.has(key)));
  }catch(error){accountsError=error.message;loggedAccounts=[];selectedAccounts.clear();}
  finally{accountsFetching=false;accountsLoading=false;lastAccountFetch=Date.now();renderState();}
}
async function launchAccounts(rows) {
  if(!rows.length){toast('请先勾选账号',true);return;}
  batchStarting=true;renderAccounts();
  try{
    const result=await api('accounts/start',{accounts:rows.map(({wxid,pid})=>({wxid,pid}))});
    const summary=`已提交 ${result.started} 项启动请求`+(result.already_running?`，${result.already_running} 项已在运行`:'')+(result.failed?`，${result.failed} 项失败`:'');
    $('#batch-result').innerHTML=`<p>${escape(summary)}</p>`+result.results.filter(row=>row.status==='error').map(row=>`<p class="account-error">${escape(rows.find(account=>account.wxid===row.wxid)?.name||row.wxid)}：${escape(row.error)}</p>`).join('');
    $('#batch-result').classList.remove('hidden');
    await loadRobots();await loadAccounts();
    await loadConfig();
  }finally{batchStarting=false;renderAccounts();}
}
function fill(form, values) {
  if (!values) return;
  for (const input of form.elements) {
    if (!(input.name in values)) continue;
    if (input.type === 'checkbox') input.checked = values[input.name] === true;
    else input.value = input.name === 'extra_body' ? JSON.stringify(values[input.name] ?? {}, null, 2) : values[input.name] ?? '';
  }
}
async function loadConfig() {
  const requested = robot, loaded = await api('config');
  if (requested !== robot) return;
  config = loaded;
  fill($('#groups-form'), {...config, groups:config.groups?.join('\n')||''});
  fill($('#scope-form'), {chat_allowlist:config.chat_allowlist.join('\n')});
  for (const name of ['llm','tts']) {
    const form = $('#'+name+'-form');
    fill(form, {...(name === 'llm' ? {provider:'openai_compatible', json_mode:true, token_limit_parameter:'max_tokens', extra_body:{}} : {}), ...config[name]});
    form.elements.api_key.value = ''; form.elements.clear_api_key.checked = false;
    const local = /^http:\/\/(127\.0\.0\.1|localhost)[:/]/.test(config[name]?.base_url || '');
    $('.key-state',form).textContent = config[name]?.api_key_configured ? '已配置' : local ? '本机服务无需密钥' : '尚未配置';
    $('.key-state',form).classList.toggle('missing', !config[name]?.api_key_configured && !local);
  }
  fill($('#delay-form'), config.text_send_delay);
  loadModels();
}
let modelRequest = 0;
async function loadModels(switched = false) {
  const f = $('#llm-form').elements, select = $('#llm-model-select'), hint = $('#llm-model-hint');
  // 订阅登录（Codex、Claude Code）不用接口地址和密钥，只能选列表里的模型。
  const subscription = ['codex','claude_code'].includes(f.provider.value), request = ++modelRequest;
  $('#llm-form').dataset.provider = f.provider.value;
  f.base_url.disabled = f.api_key.disabled = subscription;
  select.innerHTML = '<option value="">正在获取模型列表…</option>'; hint.textContent = '';
  let models = [], preferred = '';
  try { ({models, default:preferred} = await api('llm-models', {provider:f.provider.value, base_url:f.base_url.value, api_key:f.api_key.value})); }
  catch (error) { if (request === modelRequest) hint.textContent = error.message; }
  if (request !== modelRequest) return;
  // 订阅登录只接受列表内的模型；兼容接口保留列表外的自定义模型名，切换接入方式时选第一个。
  const current = f.model.value.trim();
  const fallback = models.includes(preferred) ? preferred : models[0];
  const value = models.includes(current) ? current : models.length && (subscription || switched || !current) ? fallback : '';
  select.innerHTML = models.map(m => `<option>${escape(m)}</option>`).join('') + '<option value="">手动填写…</option>';
  select.value = value;
  if (value) f.model.value = value;
  f.model.classList.toggle('hidden', Boolean(value));
}
$('#llm-model-select').onchange = e => {
  const input = $('#llm-form').elements.model;
  input.classList.toggle('hidden', Boolean(e.target.value));
  if (e.target.value) input.value = e.target.value; else input.focus();
};
$('#llm-form').elements.provider.onchange = () => loadModels(true);
// 开自动回复会一并开长期记忆；关长期记忆会一并关自动回复（自动回复离不开聊天记录）。
$('#chat-memory').onchange = e => saveChatSwitch(e.target.checked, e.target.checked && $('#chat-reply').checked);
$('#chat-reply').onchange = e => saveChatSwitch($('#chat-memory').checked || e.target.checked, e.target.checked);
$('#chat-forget').onclick = async () => {
  const chat=$('#send-chat').value,requested=robot,name=$('#chat-heading').textContent;
  if(!chat||!confirm(`清空「${name}」的长期记忆？\n机器人会忘掉以前的聊天，开关不变，之后的新消息照常记录。`))return;
  try{
    const {cleared}=await api('chat-forget',{chat_id:chat},requested);if(requested!==robot)return;
    chatHistory.delete(requested+':'+chat);historyLoading.delete(requested+':'+chat);switchHint={chat,text:`已清空 ${cleared} 条记忆`};
  }catch(error){switchHint={chat,text:error.message};}
  chatListMarkup='';chatTimelineSignature='';renderChatWorkspace();
};
$('#llm-form').elements.base_url.onchange = $('#llm-form').elements.api_key.onchange = () => loadModels();
$('#llm-load-models').onclick = () => loadModels();
const lines = value => [...new Set(value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean))];
async function save(data, message) { await api('config',data); await loadConfig(); toast(message||'配置已保存，下次启动生效'); }
$('#start-selected').onclick = () => action(()=>launchAccounts(loggedAccounts.filter(row=>selectedAccounts.has(accountKey(row))&&row.selectable&&!activeStates.includes(row.state))));
$('#refresh-accounts').onclick=()=>loadAccounts();
$('#account-list').onchange=event=>{
  const input=event.target.closest('[data-select-account]');if(!input)return;
  if(input.checked)selectedAccounts.add(input.dataset.selectAccount);else selectedAccounts.delete(input.dataset.selectAccount);
  renderAccounts();
};
$('#account-list').onclick=event=>{
  const button=event.target.closest('[data-account-action]');if(!button)return;
  const account=loggedAccounts.find(row=>accountKey(row)===button.dataset.accountAction);if(!account)return;
  if(button.dataset.action==='stop') {
    if(!account.robot_id)return;
    action(async()=>{await api('stop',{},account.robot_id);await loadRobots();await loadAccounts();toast('正在停止，等待连接清理完成');});
  } else action(()=>launchAccounts([account]));
};
$('#plugin-search').oninput = renderPlugins;
$('#plugin-list').onclick = e => {
  const button=e.target.closest('[data-plugin]');if(!button)return;
  if(button.dataset.action==='configure') {
    if(state && ['starting','running','stopping'].includes(state.state)) {toast('请先停止机器人再编辑插件选项',true);return;}
    selectedPlugin=button.dataset.plugin;$('#plugin-dialog-title').textContent=(pluginNames[selectedPlugin]?.[0]||selectedPlugin)+' · 配置';
    $('#plugin-form').elements.options.value=JSON.stringify(config?.plugins[selectedPlugin]||{},null,2);openDialog('#plugin-dialog');return;
  }
  action(async()=>{await api('plugin',{name:button.dataset.plugin,action:button.dataset.action});await loadConfig();toast('插件操作已完成');});
};
$('#close-plugin-dialog').onclick=()=>$('#plugin-dialog').close();
$('#plugin-form').onsubmit=e=>{e.preventDefault();action(async()=>{let options;try{options=JSON.parse(e.target.elements.options.value);}catch{throw new Error('JSON 格式有误，请检查逗号和引号');}await api('plugin',{name:selectedPlugin,action:'configure',options});await loadConfig();$('#plugin-dialog').close();toast('插件配置已保存');});};
$('#groups-form').onsubmit=e=>{e.preventDefault();action(()=>save({groups:lines(e.target.elements.groups.value),history_enabled:e.target.elements.history_enabled.checked,daily_clear:e.target.elements.daily_clear.checked}));};
$('#scope-form').onsubmit=e=>{e.preventDefault();action(()=>save({chat_allowlist:lines(e.target.elements.chat_allowlist.value)}));};
for (const name of ['llm','tts']) $('#'+name+'-form').onsubmit=e=>{
  e.preventDefault();
  action(()=>{
    const values={};
    for(const input of e.target.elements){if(!input.name)continue;values[input.name]=input.type==='checkbox'?input.checked:input.type==='number'?Number(input.value):input.value;}
    if(name === 'llm') {
      try { values.extra_body=JSON.parse(values.extra_body.trim() || '{}'); }
      catch { throw new Error('扩展参数的 JSON 格式有误，请检查逗号和引号'); }
      if(values.extra_body === null || Array.isArray(values.extra_body) || typeof values.extra_body !== 'object')throw new Error('扩展参数应为 JSON 对象，例如 {}');
    }
    return save({[name]:values});
  });
};
$('#delay-form').onsubmit=e=>{e.preventDefault();const f=e.target.elements;action(()=>save({text_send_delay:{enabled:f.enabled.checked,characters_per_group:Number(f.characters_per_group.value),min_seconds:Number(f.min_seconds.value),max_seconds:Number(f.max_seconds.value)}}));};
$('#discover-chats').onclick=()=>action(async()=>{
  const {chats}=await api('chats');const rooms=chats.filter(c=>c.is_group||c.chat_id?.endsWith('@chatroom'));
  $('#discovered-chats').className=rooms.length?'':'empty';$('#discovered-chats').innerHTML=rooms.map(c=>`<div class="chat-row"><div>${escape(c.name||c.chat_id)}<small>${escape(c.chat_id)}</small></div><button class="secondary small" data-room="${escape(c.chat_id)}">填入列表</button></div>`).join('')||'暂无已接收消息的群。';
});
$('#discovered-chats').onclick=e=>{const button=e.target.closest('[data-room]');if(!button)return;const field=$('#groups-form').elements.groups;field.value=[...new Set([...lines(field.value),button.dataset.room])].join('\n');toast('已填入，停止机器人后保存群聊配置');};

const roleLabels = {owner:'群主', admin:'群管理员', member:'普通群友', unknown:'身份未知'};
function sendFeedback(text, error=false) {
  $('#send-status').textContent=text;$('#send-status').classList.toggle('error',error);
}
function renderSendState() {
  const ready=serviceAvailable&&!!state?.connected&&!busy;
  $('#refresh-send-chats').disabled=!ready||sendLoading||!!pendingSend;
  $('#refresh-send-members').disabled=!ready||sendLoading||!!pendingSend||!$('#send-chat').value.endsWith('@chatroom');
  $('#send-chat').disabled=!ready||sendLoading||!!pendingSend;
  $('#send-content').disabled=!ready||!!pendingSend;
  $('#send-text').disabled=!ready||sendLoading||!!pendingSend||!sendSession||!$('#send-chat').value||!$('#send-content').value.trim();
  $('#send-text').textContent=pendingSend?'发送中…':'发送';
  $('#mention-count').textContent=`已选 ${mentionSelection.size} / 8`;
  $('#send-members').querySelectorAll('input').forEach(node=>node.disabled=!ready||!!pendingSend);
}
function renderSendMembers() {
  const query=$('#member-search').value.trim().toLowerCase();
  const rows=sendMembers.filter(row=>[row.nickname,row.wxid,roleLabels[row.role]||roleLabels.unknown].some(value=>String(value||'').toLowerCase().includes(query)));
  $('#send-members').innerHTML=rows.map(row=>`<tr><td><label class="member-choice"><input type="checkbox" data-mention="${escape(row.wxid)}" ${mentionSelection.has(row.wxid)?'checked':''} aria-label="艾特 ${escape(row.nickname||row.wxid)}"></label></td><td><span class="message-sender">${escape(row.nickname||row.wxid)}</span><span class="message-id">${escape(row.wxid)}</span></td><td class="member-role">${escape(roleLabels[row.role]||roleLabels.unknown)}</td></tr>`).join('')||'<tr><td colspan="3" class="table-empty">'+(query?'没有匹配的成员。':'暂无可用的成员，请在微信打开该群后刷新。')+'</td></tr>';
  renderSendState();
}
function resetComposer() {
  saveChatDraft();chatDraftId='';chatRows=[];chatLoadKey=robot;chatTimelineSignature='';chatConfirm=null;
  if($('#chat-send-dialog')?.open)$('#chat-send-dialog').close();
  sendGeneration++;sendMembers=[];mentionSelection.clear();sendSession='';sendLoading=false;pendingSend=null;
  $('#send-chat').innerHTML='<option value="">选择会话</option>';$('#send-content').value='';$('#member-search').value='';
  $('#send-group').classList.add('hidden');sendFeedback('');renderSendMembers();
}
async function loadSendChats() {
  const requested=robot,generation=++sendGeneration;
  sendLoading=true;sendSession='';sendFeedback('正在读取会话…');renderSendState();
  try {
    const [{chats},connection]=await Promise.all([api('chats',undefined,requested),api('send-connection',undefined,requested)]);
    if(requested!==robot||generation!==sendGeneration)return;
    const previous=$('#send-chat').value, rows=[...chats];
    if(!rows.some(row=>row.chat_id==='filehelper'))rows.unshift({chat_id:'filehelper',name:'文件传输助手'});
    chatRows=rows;
    $('#send-chat').innerHTML='<option value="">选择会话</option>'+rows.map(row=>`<option value="${escape(row.chat_id)}">${escape(row.name||row.chat_id)}${row.name?' · '+escape(row.chat_id):''}</option>`).join('');
    // 重开管理台时回到上次看的会话
    let wanted=previous;if(!wanted)try{wanted=localStorage.getItem('helper-last-chat:'+requested)||'';}catch{}
    if(rows.some(row=>row.chat_id===wanted)){$('#send-chat').value=wanted;if(wanted!==previous)restoreChatDraft(wanted);}
    sendSession=connection.session_id;sendFeedback('');
  } catch(error) {if(requested===robot&&generation===sendGeneration)sendFeedback(error.message,true);}
  finally {if(requested===robot&&generation===sendGeneration){sendLoading=false;renderSendState();}}
  if(requested===robot&&generation===sendGeneration){await selectSendChat();renderChatWorkspace();}
}
async function selectSendChat() {
  const room=$('#send-chat').value,requested=robot,generation=++sendGeneration;
  sendMembers=[];mentionSelection.clear();$('#member-search').value='';
  $('#send-group').classList.toggle('hidden',!room.endsWith('@chatroom'));renderSendMembers();
  renderChatWorkspace();
  if(!room){renderSendState();return;}
  loadChatHistory(room);
  sendSession='';sendLoading=true;sendFeedback('正在读取…');renderSendState();
  try {
    const data=await api(room.endsWith('@chatroom')?'group-members?room='+encodeURIComponent(room):'send-connection',undefined,requested);
    if(requested!==robot||generation!==sendGeneration)return;
    sendSession=data.session_id;sendMembers=data.members||[];sendFeedback('');renderSendMembers();
  } catch(error) {if(requested===robot&&generation===sendGeneration)sendFeedback(error.message,true);}
  finally {if(requested===robot&&generation===sendGeneration){sendLoading=false;renderSendState();renderChatWorkspace();}}
}
$('#refresh-send-chats').onclick=loadSendChats;
$('#send-chat').onchange=()=>{restoreChatDraft($('#send-chat').value);selectSendChat();};$('#refresh-send-members').onclick=selectSendChat;
$('#member-search').oninput=renderSendMembers;$('#send-content').oninput=()=>{saveChatDraft();renderSendState();};
$('#send-members').onchange=event=>{
  const node=event.target;if(!node.matches('[data-mention]'))return;
  if(node.checked&&mentionSelection.size>=8){node.checked=false;sendFeedback('每条消息最多艾特 8 人',true);return;}
  if(node.checked)mentionSelection.add(node.dataset.mention);else mentionSelection.delete(node.dataset.mention);
  sendFeedback('');renderSendState();
};
async function pollSendStatus() {
  const job=pendingSend;if(!job||sendPolling)return;
  sendPolling=true;
  try {
    const result=await api('send-status?id='+encodeURIComponent(job.id),undefined,job.robot);
    if(pendingSend!==job||job.robot!==robot)return;
    if(result.status==='sending')return;
    pendingSend=null;
    if(result.status==='sent'){$('#send-content').value='';chatDrafts.delete(job.robot+':'+job.chat);sendFeedback('已发送');}
    else sendFeedback(result.error||'发送未完成，请核对微信后再重发',true);
    renderSendState();
  } catch(error) {
    if(pendingSend!==job)return;
    // 服务端明确答复（如发送记录已失效）就结束等待，不让输入框和账号切换一直锁着；连不上服务时继续重查。
    if(error.status){pendingSend=null;renderSendState();}
    sendFeedback('暂时无法查询发送结果，请先核对微信。不会自动重发。',true);
  }
  finally {sendPolling=false;}
}
async function submitSendNow(event){
  event.preventDefault();if($('#send-text').disabled||pendingSend)return;
  const requested=robot,job={id:crypto.randomUUID(),robot:requested,chat:$('#send-chat').value};pendingSend=job;
  const body={request_id:job.id,session_id:sendSession,chat_id:$('#send-chat').value,content:$('#send-content').value,at_list:[...mentionSelection]};
  sendFeedback('正在发送…');renderSendState();
  try {
    await api('send-text',body,requested);
    if(pendingSend===job)await pollSendStatus();
  } catch(error) {
    if(pendingSend!==job)return;
    if(error.status){pendingSend=null;sendFeedback(error.message,true);}
    else sendFeedback('发送结果暂不确定，请先核对微信。不会自动重发。',true);
    renderSendState();
  }
};
$('#send-form').onsubmit=event=>{
  event.preventDefault();if($('#send-text').disabled||pendingSend)return;
  chatConfirm={robot,session:sendSession,chat:$('#send-chat').value,text:$('#send-content').value,mentions:[...mentionSelection]};
  $('#chat-confirm-name').textContent=chatRows.find(row=>row.chat_id===chatConfirm.chat)?.name||chatConfirm.chat;
  $('#chat-confirm-text').textContent=chatConfirm.text;
  $('#chat-send-dialog').showModal();
};
$('#chat-confirm-cancel').onclick=()=>{chatConfirm=null;$('#chat-send-dialog').close();};
$('#chat-send-dialog').addEventListener('cancel',()=>{chatConfirm=null;});
$('#chat-confirm-send').onclick=()=>{
  const value=chatConfirm;chatConfirm=null;$('#chat-send-dialog').close();
  if(!value||value.robot!==robot||value.session!==sendSession||value.chat!==$('#send-chat').value||value.text!==$('#send-content').value||JSON.stringify(value.mentions)!==JSON.stringify([...mentionSelection])){sendFeedback('账号、会话或内容已改变，未发送',true);return;}
  submitSendNow({preventDefault(){}});
};
setInterval(pollSendStatus,1500);
function saveChatDraft(){if(chatDraftId)chatDrafts.set(chatDraftId,$('#send-content').value);}
function restoreChatDraft(identifier){
  saveChatDraft();chatDraftId=identifier?robot+':'+identifier:'';
  $('#send-content').value=chatDrafts.get(chatDraftId)||'';
}
function chatAvatar(name){
  let color=0;for(const c of String(name))color=(color+c.codePointAt(0))%6;
  return `<span class="hc-avatar hc-color-${color}" aria-hidden="true">${escape([...String(name||'聊')][0])}</span>`;
}
const shortChatId=id=>String(id||'').replace(/@chatroom$/,'');
let chatNamesAt=0,switchHint={chat:'',text:''};
async function saveChatSwitch(memory,reply){
  const chat=$('#send-chat').value,requested=robot;if(!chat)return;
  try{
    const setting=await api('chat-settings',{chat_id:chat,memory,reply},requested);if(requested!==robot)return;
    const known=chatRows.find(row=>row.chat_id===chat);
    if(known)Object.assign(known,setting);else chatRows.push({chat_id:chat,...setting});
    switchHint={chat,text:setting.notice||''};
  }catch(error){switchHint={chat,text:error.message};}
  chatListMarkup='';renderChatWorkspace();
}
async function refreshChatNames(){
  // 只合并名字，不动发送状态；10 秒内最多一次。
  if(Date.now()-chatNamesAt<10000)return;
  chatNamesAt=Date.now();const requested=robot;
  try{
    const {chats}=await api('chats',undefined,requested);if(requested!==robot)return;
    const known=new Map(chatRows.map(row=>[row.chat_id,row]));
    for(const chat of chats)known.set(chat.chat_id,{...known.get(chat.chat_id),...chat});
    chatRows=[...known.values()];chatListMarkup='';chatTimelineSignature='';renderChatWorkspace();
  }catch{}
}
function renderChatWorkspace(){
  if(!$('#chat-conversations'))return;
  const connected=!!state?.connected&&serviceAvailable;
  $('.account-switch').classList.toggle('online',connected);
  if(!connected)chatLoadKey='';
  if(connected&&!busy&&!pendingSend&&!$('#page-chat').classList.contains('hidden')&&chatLoadKey!==robot){
    chatLoadKey=robot;loadSendChats();
  }
  const all=new Map(chatRows.map(row=>[row.chat_id,{...row}]));
  for(const message of messages){
    if(!message.chat_id||message.kind==='agent')continue;
    if(!all.has(message.chat_id))all.set(message.chat_id,{chat_id:message.chat_id,name:message.chat_name||message.chat_id});
    all.get(message.chat_id).last=message;
  }
  for(const row of all.values())if(!row.last&&row.preview)row.last=row.preview;  // 本次没来消息的，用库里最后一条做预览
  if(connected&&[...all.values()].some(row=>!row.name||row.name===row.chat_id))refreshChatNames();
  const selected=$('#send-chat').value,query=$('#chat-filter').value.trim().toLowerCase();
  const rows=[...all.values()].filter(row=>!query||[row.name,row.chat_id,row.last?.content].some(v=>String(v||'').toLowerCase().includes(query)));
  // 本次有消息的按到达先后排在前面；只有以前聊天的（库里预览）排在后面，按时间。
  const rank=row=>row.last?(row.last.history?1:2):0;
  rows.sort((a,b)=>rank(b)-rank(a)||(rank(a)===2?(b.last.id||0)-(a.last.id||0):(b.last?.timestamp||0)-(a.last?.timestamp||0)));
  const listMarkup=rows.map(row=>{
    const name=row.name&&row.name!==row.chat_id?row.name:shortChatId(row.chat_id),last=row.last;
    return `<button type="button" class="hc-session ${row.chat_id===selected?'selected':''}" data-chat="${escape(row.chat_id)}" aria-label="打开会话 ${escape(name)}" ${pendingSend?'disabled':''}>${chatAvatar(name)}<span class="hc-session-info"><span class="hc-session-row"><strong>${escape(name)}</strong><time>${escape(last?.history?String(last.date).slice(5)+' '+String(last.time).slice(0,5):last?.time||'')}</time></span><span class="hc-preview">${escape(last?.content||'暂无本次连接的消息')}</span></span></button>`;
  }).join('')||`<p class="hc-empty-list">${query?'没有匹配的会话':connected?'等待消息，或刷新会话':'先在账号管理中启动一个账号'}</p>`;
  if(listMarkup!==chatListMarkup){chatListMarkup=listMarkup;$('#chat-conversations').innerHTML=listMarkup;}
  const row=all.get(selected),name=row?.name&&row.name!==selected?row.name:shortChatId(selected);
  $('#chat-heading').textContent=name||'';
  // 每个会话两个开关；没连上微信时改不了，先藏起来。
  $('#chat-switches').classList.toggle('hidden',!selected||!connected);
  $('#chat-memory').checked=!!row?.memory;$('#chat-reply').checked=!!row?.reply;
  $('#chat-forget').classList.toggle('hidden',!row?.memory);
  $('#chat-switch-hint').textContent=switchHint.chat===selected?switchHint.text:'';
  $('#chat-mention-toggle').classList.toggle('hidden',!selected.endsWith('@chatroom'));
  $('#chat-mention-toggle').textContent='@'+(mentionSelection.size?' '+mentionSelection.size:'');
  const timeline=[...(chatHistory.get(robot+':'+selected)||[]),...messages.filter(message=>message.chat_id===selected)];
  const signature=JSON.stringify([robot,selected,timeline,connected]);
  if(signature===chatTimelineSignature)return;
  chatTimelineSignature=signature;
  const target=robot+':'+selected;
  const box=$('#chat-timeline'),atBottom=target!==chatTimelineTarget||box.scrollHeight-box.scrollTop-box.clientHeight<80,top=box.scrollTop;
  chatTimelineTarget=target;
  let lastTime='';
  box.innerHTML=timeline.map(message=>{
    if(message.kind==='agent')return agentTrace(message);
    const time=[message.date,message.time].filter(Boolean).join(' '),showTime=time!==lastTime;lastTime=time;
    const own=message.direction==='outgoing';
    const label=own?'我':message.sender_name||name||'对方';
    return `${showTime?`<div class="hc-time">${escape(time)}</div>`:''}<article class="hc-message ${own?'outgoing':''}">${chatAvatar(label)}<div class="hc-message-body">${message.is_group&&!own?`<div class="hc-sender">${escape(label)}</div>`:''}<div class="hc-bubble">${message.recalled?'<span class="hc-recalled">已撤回</span>':''}${escape(message.content)}</div></div></article>`;
  }).join('')||`<div class="hc-welcome"><span class="hc-welcome-icon">↔</span><h2>${selected?'等待新消息':'选择一个会话'}</h2><p>${connected?'这里只显示本次连接收到的消息，不是完整历史记录':'前往账号管理启动机器人后，即可收发消息'}</p>${!connected?'<a href="#overview">前往账号管理 →</a>':''}</div>`;
  box.scrollTop=atBottom?box.scrollHeight:top;
}
const agentLabels={start:'收到',thought:'思考',tool:'调用工具',result:'工具结果',done:'完成',error:'出错'};
const agentStatus={running:'进行中…',done:'已完成',error:'出错'};
const openAgentRuns=new Set();
function agentTrace(row){
  const steps=row.steps||[],tools=steps.filter(step=>step.kind==='tool').length;
  const open=row.status==='running'||openAgentRuns.has(row.run);
  return `<details class="hc-agent ${escape(row.status)}" data-run="${escape(row.run)}" ${open?'open':''}><summary>智能体 · ${escape(agentStatus[row.status]||row.status)}${tools?` · 调用工具 ${tools} 次`:''}</summary><ol>${steps.map(step=>`<li class="${escape(step.kind)}"><span>${escape(agentLabels[step.kind]||step.kind)}</span>${escape(step.text||'')}</li>`).join('')}</ol></details>`;
}
$('#chat-timeline').addEventListener('toggle',event=>{
  const run=event.target.dataset?.run;if(!run)return;
  if(event.target.open)openAgentRuns.add(run);else openAgentRuns.delete(run);
},true);
$('#chat-filter').oninput=renderChatWorkspace;
// 会话栏右缘可拖动调宽（200–480px），双击恢复默认；宽度只记在本机浏览器。
(()=>{
  const layout=$('.hc-layout'),handle=$('.hc-resizer');if(!layout||!handle)return;
  const set=width=>{width=Math.round(Math.max(200,Math.min(480,width)));layout.style.setProperty('--hc-list-width',width+'px');handle.setAttribute('aria-valuenow',width);return width;};
  const save=width=>{try{width?localStorage.setItem('helper-chat-list-width',width):localStorage.removeItem('helper-chat-list-width');}catch{}};
  try{const saved=Number(localStorage.getItem('helper-chat-list-width'));if(saved)set(saved);}catch{}
  handle.addEventListener('pointerdown',event=>{
    event.preventDefault();handle.setPointerCapture(event.pointerId);document.body.classList.add('resizing');
    const left=layout.getBoundingClientRect().left;let width=0;
    const move=moveEvent=>{width=set(moveEvent.clientX-left);};
    const end=()=>{handle.removeEventListener('pointermove',move);handle.removeEventListener('pointerup',end);handle.removeEventListener('pointercancel',end);document.body.classList.remove('resizing');if(width)save(width);};
    handle.addEventListener('pointermove',move);handle.addEventListener('pointerup',end);handle.addEventListener('pointercancel',end);
  });
  handle.addEventListener('keydown',event=>{
    const step={ArrowLeft:-16,ArrowRight:16}[event.key];if(!step)return;event.preventDefault();
    save(set($('.hc-list').getBoundingClientRect().width+step));
  });
  handle.addEventListener('dblclick',()=>{layout.style.removeProperty('--hc-list-width');handle.removeAttribute('aria-valuenow');save(0);});
})();
$('#chat-refresh').onclick=()=>{if(!$('#refresh-send-chats').disabled)loadSendChats();};
$('#chat-conversations').onclick=event=>{
  const button=event.target.closest('[data-chat]');if(!button||pendingSend)return;
  const id=button.dataset.chat;
  if(![...$('#send-chat').options].some(option=>option.value===id)){
    const option=document.createElement('option');option.value=id;option.textContent=id;$('#send-chat').append(option);
  }
  $('#send-chat').value=id;restoreChatDraft(id);
  try{localStorage.setItem('helper-last-chat:'+robot,id);}catch{}
  $('#page-chat').classList.add('chat-detail-open');$('#chat-conversation').classList.remove('chat-mentions-open');
  selectSendChat();
};
$('#chat-back').onclick=()=>$('#page-chat').classList.remove('chat-detail-open');
$('#chat-mention-toggle').onclick=()=>$('#chat-conversation').classList.toggle('chat-mentions-open');
$('#send-members').addEventListener('change',renderChatWorkspace);
$('#send-content').addEventListener('keydown',event=>{
  if(event.key!=='Enter'||event.isComposing||event.keyCode===229||event.shiftKey||event.ctrlKey||event.metaKey)return;
  event.preventDefault();if(!$('#send-text').disabled)$('#send-form').requestSubmit();
});
function renderMessages() {
  renderChatWorkspace();
  const query=$('#message-search').value.trim().toLowerCase();
  const rows=messages.filter(row=>row.kind!=='agent').filter(row=>!query||[row.chat_id,row.sender_id,row.sender_name,row.content,row.forwarded_by,row.forwarded_by_id,row.source_time,row.message_id,roleLabels[row.sender_role],row.recalled?'已撤回':''].some(value=>String(value||'').toLowerCase().includes(query)));
  const total=messages.filter(row=>row.kind!=='agent').length;
  $('#message-count').textContent=(query?rows.length+' / '+total:total)+' 条';
  $('#message-error').textContent=messageError;$('#message-error').classList.toggle('hidden',!messageError);
  const signature=JSON.stringify([robot,query,rows,rows.length?null:state?.state]);
  if(signature===messageSignature)return;
  messageSignature=signature;
  const scroll=$('#message-scroll'),previousTop=scroll.scrollTop;
  $('#message-list').innerHTML=rows.map(row=>`<tr><td class="message-time" title="${escape(row.date+' '+row.time)}">${escape(row.time)}</td><td><span class="message-context">${row.is_group?'群聊':'私聊'}</span><span class="message-id">${escape(row.chat_id)}</span></td><td><span class="message-sender">${escape(row.sender_name||row.sender_id||(row.forwarded?'原作者未提供':'未知发送者'))}</span>${row.sender_id?`<span class="message-id">${escape(row.sender_id)}</span>`:''}${row.is_group&&!row.forwarded&&row.sender_role&&row.sender_role!=='unknown'?`<span class="message-context">${escape(roleLabels[row.sender_role]||'')}</span>`:''}${row.forwarded?`<span class="message-context">由 ${escape(row.forwarded_by||'未知用户')} 转发</span>`:''}${row.direction==='outgoing'?'<span class="message-context">发出</span>':''}</td><td class="message-content">${row.forwarded?`<span class="message-context">转发记录 ${escape(row.forwarded_index)}${row.source_time?' · '+escape(row.source_time):''}</span>`:''}${row.recalled?`<span class="message-recalled" title="${escape(row.recall_notice||'')}">已撤回</span>`:''}${escape(row.content)}</td></tr>`).join('')||`<tr><td colspan="4" class="table-empty">${query?'没有匹配的消息。':state&&activeStates.includes(state.state)?'等待新消息…':'暂无消息。'}</td></tr>`;
  scroll.scrollTop=messagePaused?previousTop:scroll.scrollHeight;
}
$('#message-search').oninput=renderMessages;
$('#pause-messages').onclick=()=>{messagePaused=!messagePaused;$('#pause-messages').textContent=messagePaused?'继续滚动':'暂停滚动';if(!messagePaused)$('#message-scroll').scrollTop=$('#message-scroll').scrollHeight;};
$('#clear-messages').onclick=()=>{messages=[];renderMessages();};
async function pollMessages(requested,generation) {
  try {
    let tail=await api('messages?after='+messageCursor,undefined,requested);
    if(requested!==robot||generation!==messageGeneration)return;
    if(messageStream&&messageStream!==tail.stream_id&&!tail.reset) {
      tail=await api('messages?after=0',undefined,requested);
      if(requested!==robot||generation!==messageGeneration)return;
    }
    if((messageStream&&messageStream!==tail.stream_id)||tail.reset)messages=[];
    messageStream=tail.stream_id;messageCursor=tail.cursor;messageError='';
    const updated=new Map(tail.rows.map(row=>[row.id,row]));
    messages=messages.map(row=>{const next=updated.get(row.id);updated.delete(row.id);return next||row;});
    messages=[...messages,...updated.values()].slice(-1000);
    renderMessages();
  } catch(error) {
    if(requested!==robot||generation!==messageGeneration)return;
    messageError=error.status===404?'消息监控尚未加载，请重启管理台后刷新。':'消息读取失败，请稍后重试。';
    renderMessages();
  }
}
function renderLogs(){
  const level=$('#log-level').value;const allowed=level==='WARNING'?['WARNING','ERROR','CRITICAL']:['ERROR','CRITICAL'];
  const rows=logs.filter(r=>level==='ALL'||allowed.includes(r.level));const view=$('#log-list');const previousTop=view.scrollTop;
  view.innerHTML=rows.map(r=>`<div class="log-row" data-level="${escape(r.level)}"><span class="log-time">${escape(r.time)}</span><span class="log-level">${escape(r.level)}</span><span class="log-message">[${escape(r.logger)}] ${escape(r.message)}</span></div>`).join('')||'<div class="log-empty">暂无符合条件的日志。</div>';
  $('#log-count').textContent=rows.length+' 条';view.scrollTop=paused?previousTop:view.scrollHeight;
}
$('#pause-logs').onclick=()=>{paused=!paused;$('#pause-logs').textContent=paused?'继续滚动':'暂停滚动';};
$('#clear-logs').onclick=()=>{logs=[];renderLogs();};$('#log-level').onchange=renderLogs;
function serviceDisconnected() {
  serviceAvailable=false;
  $('#service-banner').textContent='连接中断，状态待更新。服务重启后请刷新页面。';
  $('#service-banner').classList.remove('hidden'); renderState();
}
async function poll(){
  if(pollBusy)return;pollBusy=true;
  const requested=robot,generation=messageGeneration;
  try{
    const [next,tail]=await Promise.all([api('state'),api('logs?after='+logCursor)]);
    if(requested!==robot||generation!==messageGeneration)return;
    state=next;serviceAvailable=true;
    $('#service-banner').classList.add('hidden');renderState();
    if(tail.cursor<logCursor)logs=[];logCursor=tail.cursor;
    if(tail.rows.length){logs=[...logs,...tail.rows].slice(-1000);renderLogs();}
    await pollMessages(requested,generation);
    if(requested!==robot||generation!==messageGeneration)return;
    if(Date.now()-lastRobotFetch>4000){lastRobotFetch=Date.now();await loadRobots();}
    if(Date.now()-lastAccountFetch>10000)await loadAccounts(true);
  }catch(error){serviceDisconnected();}
  finally{pollBusy=false;}
}
document.querySelectorAll('[data-page]').forEach(link=>link.addEventListener('click',()=>{if(link.hash===location.hash)navigate();}));
window.addEventListener('hashchange',navigate);navigate();
async function loadRobots(){
  const data=await api('robots');robotRows=data.robots;
  if(!robotRows.some(row=>row.id===robot))robot='default';
  renderState();
}
async function switchRobot(identifier){
  resetComposer();
  messageGeneration++;messages=[];messageCursor=0;messageStream='';messageError='';$('#message-search').value='';messageSignature='';renderMessages();
  robot=identifier;sessionStorage.setItem('helper-robot',robot);
  $('#robot-select').value=robot;
  state=null;config=null;logs=[];logCursor=0;selectedPlugin='';renderLogs();
  renderState();renderAccounts();
  $('#discovered-chats').className='empty';$('#discovered-chats').textContent='暂无已接收消息的群。';
  await loadConfig();await poll();navigate();
}
$('#account-menu').addEventListener('click',event=>{
  const item=event.target.closest('.menu-item');if(!item||item.disabled)return;
  if(item.dataset.robot&&item.dataset.robot!==robot){const select=$('#robot-select');select.value=item.dataset.robot;select.dispatchEvent(new Event('change'));}
  $('#account-menu').hidePopover();
});
$('#robot-select').onchange=event=>{
  const value=event.target.value;
  // 切走会丢掉发送跟踪，切回后已发出的正文又成了草稿，容易重发；头像菜单的切换也走这里。
  if(pendingSend){event.target.value=robot;toast('消息正在发送，等结果出来再切换账号',true);return;}
  action(async()=>{
    if(value.startsWith('account:')) {
      const account=loggedAccounts.find(row=>'account:'+accountKey(row)===value);
      if(!account)throw new Error('账号已退出，请刷新账号');
      const entry=await api('accounts/prepare',{wxid:account.wxid,pid:account.pid});
      await loadRobots();await loadAccounts();await switchRobot(entry.id);
    } else await switchRobot(value);
  });
};
function setNavigation(open, restoreFocus=false) {
  const mobile=matchMedia('(max-width: 800px)').matches;
  const show=mobile&&open;
  document.body.classList.toggle('nav-open',show);
  $('#primary-sidebar').inert=mobile&&!show;
  $('#nav-backdrop').classList.toggle('hidden',!show);
  $('#nav-toggle').setAttribute('aria-expanded',String(show));
  $('#nav-toggle').setAttribute('aria-label',show?'关闭导航':'展开导航');
  if(show)$('#primary-sidebar [aria-current="page"]')?.focus();
  else if(restoreFocus)$('#nav-toggle').focus();
}
$('#nav-toggle').onclick=()=>setNavigation(!document.body.classList.contains('nav-open'));
$('#nav-backdrop').onclick=()=>setNavigation(false,true);
document.addEventListener('keydown',e=>{
  if(!document.body.classList.contains('nav-open'))return;
  if(e.key==='Escape'){setNavigation(false,true);return;}
  if(e.key==='Tab'){
    const links=[...$('#primary-sidebar').querySelectorAll('a')];
    if(e.shiftKey&&document.activeElement===links[0]){e.preventDefault();links.at(-1).focus();}
    else if(!e.shiftKey&&document.activeElement===links.at(-1)){e.preventDefault();links[0].focus();}
  }
});
matchMedia('(max-width: 800px)').addEventListener('change',()=>setNavigation(false));setNavigation(false);
document.addEventListener('invalid',e=>{const group=e.target.closest('details');if(group)group.open=true;},true);
(async()=>{renderState();try{await loadRobots();await loadConfig();await loadAccounts();await poll();}catch(error){serviceDisconnected();toast(error.message,true);}setInterval(poll,2000);})();

  }, {once:true});
})();
