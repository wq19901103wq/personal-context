'use strict';

function renderBriefings(){
  const value=state.briefings;if(!value)return;
  const {config,today,pending,history}=value;
  $('briefing-updated').textContent=today.day+' · 按当前保存的安排更新';
  const todayBox=$('briefing-today');todayBox.replaceChildren();
  function appendItem(box,item){
    const row=element('div',undefined,'briefing-item');
    row.append(element('p',item.text,'briefing-text'));
    const source=(item.kind==='task'?state.tasks:state.events).find(v=>v.id===item.id);
    if(source)row.append(button('查看与调整',()=>item.kind==='task'?editTask(source):editEvent(source),'small quiet'));
    box.append(row);
  }
  for(const section of today.sections){
    const box=element('section',undefined,'briefing-section');box.append(element('h3',section.title));
    if(!section.items.length)box.append(element('p','暂无记录','muted'));
    for(const item of section.items)appendItem(box,item);
    todayBox.append(box);
  }
  const changes=$('briefing-changes');changes.replaceChildren();
  if(pending.length){changes.append(element('h3','待合并的变化'));for(const item of pending)appendItem(changes,item);}
  const statuses={pending:'等待合适时段',sending:'正在推送',delivered:'微信接口已接受',retry:'等待重试',unknown:'结果未知，请查看微信',failed:'推送失败',expired:'当天未发送，已过期',disabled:'推送已暂停',superseded:'变化已撤销'};
  const latest=history.find(v=>v.kind==='daily'&&v.day===today.day);
  if(latest)todayBox.append(element('p','今日微信简报：'+(statuses[latest.status]||latest.status),'muted'));
  const recent=history.filter(v=>v.kind==='changes'&&!['superseded','expired'].includes(v.status));
  if(recent.length){
    const item=recent[0];changes.append(element('h3','最近一次变化'),element('p',stamp(item.sent_at||item.created_at)+' · '+(statuses[item.status]||item.status),'muted'),element('p',item.body,'briefing-text'));
    if(item.error)changes.append(element('p',item.error,'error'));
  }
  for(const [id,key,on,off] of [['daily-toggle','daily_enabled','暂停每日推送','开启每日推送'],['changes-toggle','changes_enabled','暂停变化提醒','开启变化提醒']]){
    const b=$(id);b.textContent=config[key]?on:off;b.onclick=async()=>{b.disabled=true;try{await api('/api/briefings',{[key]:!config[key]});await refresh();}catch(e){toast(e.message);}finally{b.disabled=false;}};
  }
  $('briefing-channel').textContent=state.wechat?'使用当前绑定的微信。暂停推送后，仍可在这里查看简报。':'当前未连接微信；简报保存在这里。';
  const box=$('briefing-history'),opened=new Set([...box.querySelectorAll('details[open]')].map(d=>d.dataset.id));box.replaceChildren();
  for(const item of history){
    const d=element('details');d.dataset.id=String(item.id);d.open=opened.has(String(item.id));d.append(element('summary',item.title+' · '+(statuses[item.status]||item.status)),element('p',item.body,'briefing-text'));
    if(item.error)d.append(element('p',item.error,'error'));box.append(d);
  }
}
