(function(global) {
  'use strict';
  const t=(key,values)=>SakiI18n.t(key,values), $=id=>document.getElementById(id);
  const tasks={chat:'聊天',translation:'翻译',vision:'识图',gen_persona:'人设',call:'语音通话',moments:'朋友圈',summary:'记忆',image:'生图',forum:'论坛'};
  const names={gemini:'Gemini',openai:'ChatGPT',anthropic:'Claude',xai:'Grok',custom:'自填写'};
  let config=null,draft=null,route=null,saving=false,credentials={},keyChanges={};
  const credentialFor=r=>r==='custom'?'openrouter':r;
  const message=text=>{$('model-settings-message').textContent=text;};
  const clone=value=>JSON.parse(JSON.stringify(value));
  async function jsonFetch(url,options) {
    const response=await fetch(url,{credentials:'same-origin',...options});
    const result=await response.json();
    if(!response.ok)throw new Error(result.error||result.message||t('保存失败'));
    return result;
  }
  function capture() {
    if(!draft||!route)return;
    for(const task of Object.keys(tasks)){
      const select=$('model-'+task);
      draft.routes[route].models[task]=select.value==='__custom__'?$('custom-model-'+task).value.trim():select.value;
    }
    draft.image_daily_limit=Number($('image-daily-limit').value);
    const key=$('route-key-input').value.trim();
    if(key)keyChanges[route]=key;
    else if(keyChanges[route])delete keyChanges[route];
    if(route==='custom')Object.assign(draft.routes.custom,{name:$('custom-route-name').value.trim(),base_url:$('custom-route-url').value.trim(),protocol:$('custom-route-protocol').value});
  }
  function keyStatus() {
    const state=credentials[credentialFor(route)];
    $('route-key-status').textContent=keyChanges[route]===null?t('保存后删除此 Key'):keyChanges[route]?t('新 Key 将在保存后生效'):state?.configured?t('已保存 Key，末四位：{last}',{last:state.last_four||'••••'}):t('未配置');
  }
  function renderOverview() {
    $('image-limit-overview').textContent=t('每日生图上限：{limit} 张',{limit:config.image_daily_limit});
    const grid=$('model-settings-overview');grid.replaceChildren();
    for(const [label,value] of [[t('当前执行线路'),t(names[config.active_route])],...Object.entries(tasks).map(([task,label])=>[t(label),config.routes[config.active_route].models[task]||t('未启用')])]){
      const cell=document.createElement('div');cell.className='api-status-item';
      const title=document.createElement('div');title.className='api-label';title.textContent=label;
      const badge=document.createElement('div');badge.className='api-badge';badge.textContent=value;
      cell.append(title,badge);grid.append(cell);
    }
  }
  function renderRoute(next) {
    route=next;draft.active_route=next;$('radio-'+next).checked=true;
    $('custom-route-settings').hidden=next!=='custom';
    const advice=draft.provider_advice?.[next];
    $('provider-model-advice').hidden=next==='custom';
    $('provider-model-advice-text').textContent=t(next==='gemini'?'Pro 偏能力，Flash 偏均衡，Flash-Lite 偏速度。不同代际请结合具体模型能力选择。':advice?.text||'');
    $('provider-model-source').href=next==='gemini'?'https://ai.google.dev/gemini-api/docs/models':advice?.url||'#';
    const catalog=draft.model_catalog?.[next]||{};
    $('custom-route-name').value=draft.routes.custom.name;
    $('custom-route-url').value=draft.routes.custom.base_url;
    $('custom-route-protocol').value=draft.routes.custom.protocol;
    $('route-key-label').textContent=t(names[next])+' API Key';
    const updateEndpoint=()=>{$('route-endpoint-notice').textContent=t('实际接收地址：{url}',{url:next==='custom'?$('custom-route-url').value:draft.routes[next].base_url});};
    $('custom-route-url').oninput=updateEndpoint;updateEndpoint();
    $('route-key-input').value=keyChanges[next]||'';keyStatus();
    const container=$('model-task-fields');container.replaceChildren();
    for(const [task,label] of Object.entries(tasks)){
      const card=document.createElement('div');card.className='model-task-card';
      const heading=document.createElement('label');heading.className='model-task-title';heading.htmlFor='model-'+task;heading.textContent=t(label);
      if(next==='gemini'&&['moments','summary'].includes(task)){
        const tag=document.createElement('span');tag.className='model-recommendation';tag.textContent=t('推荐 Flash');heading.append(tag);
      }
      const select=document.createElement('select');select.id='model-'+task;select.className='model-select';
      const imageOptions=next==='custom'?Array.from(new Set(Object.values(draft.image_options).flat())):(draft.image_options[next]||[]);
      let options=task==='image'?imageOptions:(draft.model_options[next]||[]).filter(m=>task==='image'||!imageOptions.includes(m));
      options=Array.from(new Set(options));
      if(task==='image')select.add(new Option(t(next==='anthropic'?'暂不支持生图':'不使用生图'),''));
      const recommended=draft.recommendations[next][task];
      const groups={};
      if(next==='gemini'&&task!=='image'){
        for(const tier of ['Pro · 能力优先','Flash · 均衡','Flash-Lite · 速度优先','其他模型']){
          const group=document.createElement('optgroup');group.label=t(tier);groups[tier]=group;
        }
      }
      for(const model of options){
        const option=new Option(model+(model===recommended?' · '+t('推荐'):''),model);
        let tier=catalog[model]?.group;
        if(next==='gemini'&&task!=='image')tier=tier||(model.includes('flash-lite')?'Flash-Lite · 速度优先':model.includes('flash')?'Flash · 均衡':model.includes('pro')?'Pro · 能力优先':'其他模型');
        if(next!=='custom'&&Object.keys(catalog).length&&!tier)tier='其他模型';
        if(tier){
          if(!groups[tier]){groups[tier]=document.createElement('optgroup');groups[tier].label=t(tier);}
          groups[tier].append(option);
        }else select.add(option);
      }
      for(const group of Object.values(groups))if(group.children.length)select.append(group);
      const custom=document.createElement('input');custom.id='custom-model-'+task;custom.className='model-select';custom.placeholder=t('输入模型 ID');custom.setAttribute('aria-label',t(label)+' · '+t('自填写'));custom.maxLength=200;
      const value=draft.routes[next].models[task];
      if(next==='anthropic'&&task==='image'){select.disabled=true;custom.hidden=true;select.value='';}
      else {
        select.add(new Option(t('自填写'),'__custom__'));
        select.value=options.includes(value)||(task==='image'&&!value)?value:'__custom__';
        custom.value=value;custom.hidden=select.value!=='__custom__';
      }
      select.onchange=()=>{custom.hidden=select.value!=='__custom__';if(!custom.hidden)custom.focus();};
      const reset=document.createElement('button');reset.type='button';reset.className='model-reset';reset.textContent=t('恢复推荐值');reset.onclick=()=>{capture();draft.routes[route].models[task]=recommended;renderRoute(route);};
      card.append(heading,select,custom);
      if(task==='image'){
        const quotaLabel=document.createElement('label');quotaLabel.className='model-quota-label';quotaLabel.htmlFor='image-daily-limit';quotaLabel.textContent=t('每日生图上限');
        const quota=document.createElement('input');quota.id='image-daily-limit';quota.className='model-select';quota.type='number';quota.min='1';quota.max='2147483647';quota.step='1';quota.value=draft.image_daily_limit;quota.inputMode='numeric';
        const hint=document.createElement('p');hint.textContent=t('各线路共用此上限；生图模型留空可关闭。');
        card.append(quotaLabel,quota,hint);
      }
      card.append(reset);container.append(card);
    }
  }
  global.selectRoute=next=>{if(saving)return;capture();renderRoute(next);};
  global.toggleApiEdit=editing=>{
    if(saving||!config)return;
    keyChanges={};
    $('api-view-mode').hidden=editing;$('api-edit-mode').hidden=!editing;
    if(editing){draft=clone(config);renderRoute(config.active_route);}
    else{$('route-key-input').value='';draft=null;route=null;}
    message('');
  };
  global.stageRouteKeyDelete=()=>{keyChanges[route]=null;$('route-key-input').value='';keyStatus();};
  $('route-key-input').addEventListener('input',()=>{keyChanges[route]=$('route-key-input').value.trim()||undefined;keyStatus();});
  global.saveApiConfig=async()=>{
    if(saving)return;
    capture();
    if(!Number.isInteger(draft.image_daily_limit)||draft.image_daily_limit<1||draft.image_daily_limit>2147483647){message(t('每日生图上限需为正整数'));$('image-daily-limit').focus();return;}
    if(draft.active_route==='custom'&&(config.active_route!=='custom'||draft.routes.custom.base_url!==config.routes.custom.base_url||keyChanges.custom)){
      if(!confirm(t("将向此接收地址发送 Key 和模型请求内容：\n{url}\n请确认这是你信任的服务。",{url:draft.routes.custom.base_url})))return;
    }
    saving=true;
    const controls=Array.from($('api-edit-mode').querySelectorAll('button,input,select'));const disabled=controls.map(e=>e.disabled);controls.forEach(e=>e.disabled=true);message(t('保存中...'));
    try{
      const result=await jsonFetch('/api/system_config',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'XMLHttpRequest'},body:JSON.stringify({...draft,credential_updates:Object.fromEntries(Object.entries(keyChanges).filter(([,v])=>v!==undefined).map(([r,v])=>[credentialFor(r),v]))})});
      config=result.config;credentials=result.credentials||credentials;renderOverview();
      saving=false;global.toggleApiEdit(false);message(t('API 配置已更新！'));
    }catch(error){message(error.message);}finally{saving=false;controls.forEach((e,i)=>e.disabled=disabled[i]);}
  };
  async function load(){
    try{
      const [settings,user]=await Promise.all([jsonFetch('/api/system_config'),jsonFetch('/api/user/profile_settings')]);
      config=settings;credentials=user.credentials||{};renderOverview();
    }catch(error){message(error.message);}
  }
  load();
})(window);
