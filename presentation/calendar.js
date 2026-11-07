'use strict';
let weekStart=null, selectedDay=null, editingEvent=null;
let repeatDays=new Set();
function dateKey(value,tz=state.sources.timezone||'Asia/Shanghai'){
  const parts=new Intl.DateTimeFormat('en-CA',{timeZone:tz,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date(value*1000));
  const get=k=>parts.find(p=>p.type===k).value;
  return get('year')+'-'+get('month')+'-'+get('day');
}
function plusDays(key,days){const d=new Date(key+'T00:00:00Z');d.setUTCDate(d.getUTCDate()+days);return d.toISOString().slice(0,10);}
function monday(key){const day=new Date(key+'T00:00:00Z').getUTCDay();return plusDays(key,-((day+6)%7));}
function clock(value){return new Intl.DateTimeFormat('zh-CN',{timeZone:state.sources.timezone||'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(value*1000));}
function periodLabel(period){return {morning:'上午',noon:'中午',afternoon:'下午',evening:'晚上',night:'深夜'}[period]||'';}
function eventDate(event){return event.starts_at==null?event.scheduled_date:dateKey(event.starts_at,event.all_day?event.timezone:undefined);}
function eventRange(event){
  if(event.starts_at==null)return [event.scheduled_date,event.scheduled_date];
  // All-day dates stay on their original local date when the display timezone changes.
  const tz=event.all_day?event.timezone:(state.sources.timezone||'Asia/Shanghai');
  return [dateKey(event.starts_at,tz),dateKey(event.ends_at==null?event.starts_at:event.ends_at-0.001,tz)];
}
function eventTime(event,key){
  if(event.starts_at==null)return event.scheduled_date?(periodLabel(event.time_period)||'当天')+' · 具体时间待定':event.time_text||'时间待定';
  if(event.all_day)return '全天';
  const [first,last]=eventRange(event);
  const end=event.ends_at==null?'':(dateKey(event.ends_at)!==last?'24:00':clock(event.ends_at));
  if(first!==last){
    if(key===first)return clock(event.starts_at)+' 起 · 跨日';
    if(key===last)return '至 '+end;
    return '跨日安排';
  }
  return clock(event.starts_at)+(event.ends_at==null?'':' – '+end);
}
function recurrenceUnresolved(event){const r=event.recurrence;return r&&r.frequency==='weekly'&&(r.weekly_count||0)>r.weekdays.length;}
function recurrenceLabel(event){
  const r=event.recurrence;if(!r)return '';
  if(r.frequency==='daily')return '每天重复';
  const days=r.weekdays.map(d=>['一','二','三','四','五','六','日'][d]).join('、');
  return recurrenceUnresolved(event)?'每周 '+r.weekly_count+' 天 · '+(days?'周'+days+'已定 · ':'')+'其余星期待定':'每周'+days+'重复';
}
function calendarCard(item,kind,key){
  const isTask=kind==='task';
  const card=button('',()=>isTask?openTask(item):openEvent(item),'calendar-card'+(isTask?' deadline':''));
  card.append(element('span',isTask?(item.due_at==null?'待办 · 未定期限':'截止 '+clock(item.due_at)):eventTime(item,key),'cal-time'),element('span',isTask?item.description:item.title,'cal-title'));
  const meta=isTask?(item.status==='waiting'?'等待回复':'待办'):([item.status==='tentative'?'待确认':'',item.location,recurrenceLabel(item),item.remind_before_minutes!=null?'提前 '+item.remind_before_minutes+' 分钟提醒':''].filter(Boolean).join(' · '));
  if(meta)card.append(element('span',meta,'cal-meta'));
  return swipeItem(card,item,kind);
}
async function renderCalendar(){
  const today=dateKey(Date.now()/1000);
  if(!weekStart){weekStart=monday(today);selectedDay=today;}
  const days=Array.from({length:7},(_,i)=>plusDays(weekStart,i));
  if(!days.includes(selectedDay))selectedDay=weekStart;
  $('calendar-zone').textContent=state.sources.timezone||'Asia/Shanghai';
  $('week-label').textContent=weekStart.replaceAll('-','.')+' — '+days[6].slice(5).replace('-','.');
  const activeTasks=state.tasks.filter(t=>!['done','cancelled'].includes(t.status));
  const activeEvents=state.events.filter(e=>e.status!=='cancelled');
  const datedEvents=await api('/api/occurrences?start='+days[0]+'&end='+plusDays(days[6],1));
  if(weekStart!==days[0])return;
  const picker=$('day-picker'),grid=$('calendar-days');picker.replaceChildren();grid.replaceChildren();
  let count=new Set();
  for(let i=0;i<7;i++){
    const key=days[i],dayName=['一','二','三','四','五','六','日'][i];
    const pick=button('',async()=>{selectedDay=key;await renderCalendar();},(key===selectedDay?'selected ':'')+(key===today?'is-today':''));
    pick.setAttribute('aria-label',key);pick.setAttribute('aria-pressed',String(key===selectedDay));
    pick.append(element('span','周'+dayName),element('b',String(Number(key.slice(8)))));picker.append(pick);
    const day=element('div',undefined,'calendar-day'+(key===today?' is-today':'')+(key===selectedDay?' selected':''));
    const heading=element('h3',undefined,'day-heading');heading.append(element('span','周'+dayName),element('b',key.slice(5).replace('-','/')));day.append(heading);
    const entries=[];
    for(const e of datedEvents){const [first,last]=eventRange(e);if(key>=first&&key<=last)entries.push({item:e,kind:'event',at:e.all_day?-Infinity:e.starts_at??Infinity});}
    for(const t of activeTasks){if(t.due_at!=null&&dateKey(t.due_at)===key)entries.push({item:t,kind:'task',at:t.due_at});}
    entries.sort((a,b)=>a.at-b.at||a.item.id-b.item.id);
    for(const entry of entries){day.append(calendarCard(entry.item,entry.kind,key));count.add(entry.kind+(entry.item.occurrence_key||entry.item.id));}
    if(!entries.length)day.append(element('p','暂无安排','day-empty'));
    grid.append(day);
  }
  $('week-count').textContent='本周 '+count.size+' 项';
  const pending=[...activeEvents.filter(e=>!eventDate(e)).map(item=>({item,kind:'event'})),...activeTasks.filter(t=>t.due_at==null).map(item=>({item,kind:'task'}))];
  $('unscheduled-count').textContent=pending.length+' 项';$('unscheduled-items').replaceChildren();
  for(const p of pending)$('unscheduled-items').append(calendarCard(p.item,p.kind));
  if(!pending.length)$('unscheduled-items').append(element('p','暂时没有待安排事项。','muted'));
  const cancelled=state.events.filter(e=>e.status==='cancelled');$('cancelled-count').textContent=cancelled.length+' 项';$('cancelled-events').hidden=!cancelled.length;$('cancelled-items').replaceChildren();
  for(const e of cancelled)$('cancelled-items').append(calendarCard(e,'event',eventDate(e)));
}
function setEventInputType(allDay){
  for(const id of ['event-start','event-end']){const field=$(id),value=field.value;field.type=allDay?'date':'datetime-local';field.value=value?(allDay?value.slice(0,10):(value.length===10?value+'T00:00':value)):'';}
}
function editEvent(event=null){
  if(event?.recurrence)event=state.events.find(e=>e.id===event.id)||event;
  editingEvent=event;$('event-form').reset();$('event-error').textContent='';$('event-evidence').replaceChildren();
  $('event-date').value=event?.scheduled_date||'';$('event-period').value=event?.time_period||'';
  $('event-dialog-title').textContent=event?.recurrence?'编辑整组日程':event?'编辑日程':'新建日程';
  repeatDays=new Set(event?.recurrence?.weekdays||[]);
  $('event-repeat').value=event?.recurrence?.frequency||'';
  $('event-weekly-count').value=event?.recurrence?.weekly_count||'';
  $('event-repeat-until').value=event?.recurrence?.until||'';
  renderRepeat();
  const tz=event?.timezone||state.sources.timezone||'Asia/Shanghai';$('event-zone').textContent='时间所在时区：'+tz;
  $('event-all-day').checked=event?.all_day||false;setEventInputType(event?.all_day||false);
  $('event-remind-enabled').checked=!event||event.remind_before_minutes!=null;
  $('event-remind-minutes').value=event?.remind_before_minutes??30;
  $('event-remind-minutes').disabled=!$('event-remind-enabled').checked;
  const reminder=event?.reminder,labels={pending:'等待微信推送',retry:'稍后重试',sending:'正在推送',delivered:'微信接口已接受提醒',failed:'提醒发送失败',unknown:'发送结果未知，请查看微信',superseded:'旧提醒已停止，按最新日程安排',expired:'已过开始时间，不再补发'};
  $('event-reminder-state').textContent=reminder?[(labels[reminder.status]||reminder.status),reminder.error].filter(Boolean).join(' · '):'';
  $('event-cancel').hidden=!event||event.status==='cancelled';
  if(event){
    $('event-title').value=event.title;$('event-notes').value=event.notes;$('event-location').value=event.location;$('event-time-text').value=event.time_text;$('event-status').value=event.status;
    $('event-start').value=event.all_day?(event.starts_at==null?'':dateKey(event.starts_at,tz)):localValue(event.starts_at,tz);
    $('event-end').value=event.all_day?(event.ends_at==null?'':dateKey(event.ends_at-0.001,tz)):localValue(event.ends_at,tz);
    if(event.evidence?.length)$('event-evidence').append(recordEvidence(event.evidence,'event-'+event.id));
  }
  $('event-more').open=!!event;editorQuickDates('event',event);$('event-dialog').showModal();
}
$('week-prev').onclick=()=>{weekStart=plusDays(weekStart,-7);selectedDay=plusDays(selectedDay,-7);renderCalendar().catch(e=>toast(e.message));};
$('week-next').onclick=()=>{weekStart=plusDays(weekStart,7);selectedDay=plusDays(selectedDay,7);renderCalendar().catch(e=>toast(e.message));};
$('week-today').onclick=()=>{selectedDay=dateKey(Date.now()/1000);weekStart=monday(selectedDay);renderCalendar().catch(e=>toast(e.message));};
$('new-event').onclick=()=>openEvent();
$('event-all-day').onchange=()=>setEventInputType($('event-all-day').checked);
$('event-remind-enabled').onchange=()=>{$('event-remind-minutes').disabled=!$('event-remind-enabled').checked;};
$('event-cancel').onclick=async()=>{const b=$('event-cancel');b.disabled=true;try{await api('/api/events/'+editingEvent.id,{status:'cancelled'});$('event-dialog').close();await refresh();toast(editingEvent.recurrence?'整组日程已取消':'日程已取消');}catch(error){$('event-error').textContent=error.message;}finally{b.disabled=false;}};
$('event-form').onsubmit=async event=>{
  event.preventDefault();const b=event.submitter;b.disabled=true;
  try{
    const allDay=$('event-all-day').checked,start=$('event-start').value,end=$('event-end').value;
    const data={title:$('event-title').value,notes:$('event-notes').value,location:$('event-location').value,time_text:$('event-time-text').value,status:$('event-status').value,all_day:allDay,scheduled_date:$('event-date').value||null,time_period:$('event-period').value,starts_at:start?(allDay?start+'T00:00':start):null,ends_at:end?(allDay?plusDays(end,1)+'T00:00':end):null};
    data.recurrence=$('event-repeat').value?{frequency:$('event-repeat').value,weekdays:[...repeatDays].sort(),until:$('event-repeat-until').value||null}:null;
    if(data.recurrence?.frequency==='weekly'&&$('event-weekly-count').value)data.recurrence.weekly_count=Number($('event-weekly-count').value);
    data.remind_before_minutes=$('event-remind-enabled').checked?Number($('event-remind-minutes').value):null;
    await api(editingEvent?'/api/events/'+editingEvent.id:'/api/events',data);$('event-dialog').close();await refresh();toast('日程已保存');
  }catch(error){$('event-error').textContent=error.message;}finally{b.disabled=false;}
};

function renderRepeat(){
  const weekly=$('event-repeat').value==='weekly';
  $('event-repeat-weekly').hidden=!weekly;$('event-series-note').hidden=!$('event-repeat').value;
  $('event-weekdays').replaceChildren(...['一','二','三','四','五','六','日'].map((day,index)=>{
    const b=button('周'+day,()=>{if(repeatDays.has(index))repeatDays.delete(index);else repeatDays.add(index);renderRepeat();},repeatDays.has(index)?'selected':'');
    b.setAttribute('aria-pressed',String(repeatDays.has(index)));return b;
  }));
  const count=Number($('event-weekly-count').value)||repeatDays.size;
  $('event-repeat-note').textContent=count>repeatDays.size?'已确定 '+repeatDays.size+' 天，其余 '+(count-repeatDays.size)+' 天的星期待定；已有时间保留在日历中，推荐会避开可能占用的时段。':'点选重复星期。';
}
$('event-repeat').onchange=()=>{
  if($('event-repeat').value==='weekly'&&!repeatDays.size){const day=$('event-start').value.slice(0,10)||$('event-date').value||selectedDay;repeatDays.add((new Date(day+'T00:00:00Z').getUTCDay()+6)%7);}
  renderRepeat();
};
$('event-weekly-count').onchange=renderRepeat;
