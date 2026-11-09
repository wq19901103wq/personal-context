'use strict';

function showToast(text, undo){
  const box=$('toast');clearTimeout(toastTimer);box.replaceChildren(element('span',text));box.hidden=false;
  if(undo)box.append(button('撤销',async()=>{await undo();box.hidden=true;},'toast-undo'));
  toastTimer=setTimeout(()=>box.hidden=true,undo?8000:4500);
}
function itemPath(item,kind){return '/api/'+(kind==='task'?'tasks/':'events/')+item.id;}
async function deleteItem(item,kind){
  await api(itemPath(item,kind),{deleted:true});
  $('item-actions').close();await refresh();
  showToast((kind==='task'?'待办':item.recurrence?'整组重复日程':'日程')+'已删除',async()=>{
    await api(itemPath(item,kind),{deleted:false});await refresh();
  });
}
function swipeItem(content,item,kind){
  if(item.read_only)return content;
  const wrapper=element('div',undefined,'swipe-item');
  wrapper.dataset.kind=kind;wrapper.dataset.id=item.id;
  const backdrop=element('div',item.recurrence?'删除整组':'删除','swipe-backdrop');backdrop.setAttribute('aria-hidden','true');
  content.classList.add('swipe-content');wrapper.append(backdrop,content);
  let origin=null,locked=false,dx=0,suppressUntil=0,busy=false;
  const reset=()=>{content.style.transform='';wrapper.classList.remove('swiping','swipe-ready');origin=null;locked=false;dx=0;};
  wrapper.addEventListener('pointerdown',event=>{
    if(busy||event.isPrimary===false||event.button!==0||event.target.closest('a,input,select,textarea,.check'))return;
    origin={x:event.clientX,y:event.clientY};dx=0;locked=false;
  });
  wrapper.addEventListener('pointermove',event=>{
    if(!origin)return;
    const x=event.clientX-origin.x,y=event.clientY-origin.y;
    if(!locked){
      if(Math.abs(y)>12&&Math.abs(y)>Math.abs(x)){origin=null;return;}
      if(Math.abs(x)<12)return;
      if(x<0||Math.abs(x)<Math.abs(y)*1.3){origin=null;return;}
      locked=true;wrapper.setPointerCapture(event.pointerId);wrapper.classList.add('swiping');
    }
    event.preventDefault();dx=Math.max(0,Math.min(x,wrapper.clientWidth*.8));
    content.style.transform='translateX('+dx+'px)';
    wrapper.classList.toggle('swipe-ready',dx>=Math.min(100,wrapper.clientWidth*.35));
  });
  wrapper.addEventListener('pointerup',async()=>{
    if(!origin)return;
    const shouldDelete=locked&&dx>=Math.min(100,wrapper.clientWidth*.35);
    if(locked)suppressUntil=Date.now()+500;
    reset();
    if(shouldDelete){busy=true;try{await deleteItem(item,kind);}catch(error){toast(error.message);}finally{busy=false;}}
  });
  wrapper.addEventListener('pointercancel',()=>{if(locked)suppressUntil=Date.now()+500;reset();});
  wrapper.addEventListener('click',event=>{
    if(Date.now()<suppressUntil){event.preventDefault();event.stopImmediatePropagation();}
  },true);
  return wrapper;
}

let actionItem=null,actionKind=null,chosenDate=null,pickerMonth=null;
function actionZone(){return actionItem.timezone||state.sources.timezone||'Asia/Shanghai';}
function actionTime(){return actionKind==='task'?actionItem.due_at:actionItem.starts_at;}
function actionDate(){return actionTime()==null?(actionKind==='event'?actionItem.scheduled_date:null):dateKey(actionTime(),actionZone());}
function openTask(task=null){if(task)openItemActions(task,'task');else editTask();}
function openEvent(event=null){if(event)openItemActions(event,'event');else editEvent();}
function editorQuickDates(kind,item){
  const target=$(kind+'-quick-schedule');if(!target)return;
  target.replaceChildren();target.hidden=!!item;if(item)return;
  const today=dateKey(Date.now()/1000),dates=element('div',undefined,'date-choices');
  const summary=element('p','未设日期','muted microcopy'),times=element('div',undefined,'time-choices');times.hidden=true;
  let selected=null,chosenTime=null;
  const apply=()=>{
    const task=kind==='task';
    if(task){$('due').value=selected?selected+'T23:59':'';summary.textContent=selected?'截止 '+selected+' 23:59':'未设截止日期';return;}
    $('event-all-day').checked=chosenTime==='all-day';setEventInputType(chosenTime==='all-day');
    $('event-start').value=selected&&chosenTime?(chosenTime==='all-day'?selected:selected+'T'+chosenTime):'';
    $('event-date').value=selected||'';$('event-period').value='';
    $('event-end').value='';$('event-time-text').value=selected&&!chosenTime?selected+'，具体时间待定':'';
    summary.textContent=selected?selected+' · '+(chosenTime==='all-day'?'全天':chosenTime||'请选择时间，或保持待定'):'时间待定';
  };
  for(const [label,key] of [['今天',today],['明天',plusDays(today,1)],['下周一',plusDays(monday(today),7)],['暂不安排',null]]){
    const b=button(label,()=>{
      selected=key;for(const c of dates.children)c.classList.toggle('selected',c===b);
      times.hidden=!key||kind==='task';apply();
    },'date-choice');dates.append(b);
  }
  for(const [label,value] of [['上午 9:00','09:00'],['下午 2:00','14:00'],['晚上 7:00','19:00'],['全天','all-day']]){
    const b=button(label,()=>{chosenTime=value;for(const c of times.children)c.classList.toggle('selected',c===b);apply();});times.append(b);
  }
  target.append(dates,summary,times);
}
function openItemActions(item,kind){
  actionItem=item;actionKind=kind;chosenDate=null;pickerMonth=null;
  const task=kind==='task',closed=['done','cancelled'].includes(item.status),dialog=$('item-actions');
  const recurring=!task&&!!item.recurrence;
  const readOnly=!!item.read_only;
  dialog.querySelector('.schedule-section').hidden=recurring||readOnly;
  dialog.querySelector('.sheet-footer').hidden=readOnly;
  $('action-source').hidden=!readOnly;
  $('action-source').textContent=readOnly?'来自 Apple 日历 · '+(item.source?.name||'')+'。请在 Apple 日历中修改或取消，助理会自动更新。':'';
  $('action-edit').textContent=recurring?'编辑整组日程':'编辑文字与更多';
  $('action-delete').textContent=recurring?'删除整组':'删除';
  $('action-kind').textContent=task?'待办':recurring?'重复日程 · 整组操作':'日程';$('action-title').textContent=task?item.description:item.title;
  const when=task?(item.due_at==null?'未设截止时间':'截止 '+stamp(item.due_at,item.timezone)):
    (item.starts_at==null?(item.scheduled_date?item.scheduled_date+' · '+eventTime(item,item.scheduled_date):item.time_text||'时间待定'):dateKey(item.starts_at,item.timezone)+' · '+eventTime(item,dateKey(item.starts_at,item.timezone)));
  $('action-meta').textContent=[when,task?(item.status==='waiting'?'等待回复':''):item.location,recurring?recurrenceLabel(item):''].filter(Boolean).join(' · ');
  $('action-primary').hidden=readOnly||(!task&&item.status==='confirmed');
  $('action-primary').textContent=task?(closed?'重新打开待办':'✓ 完成待办'):(item.status==='cancelled'?'恢复日程':'✓ 确认日程');
  $('action-primary').onclick=()=>runAction(async()=>{
    const data={status:task?(closed?'open':'done'):'confirmed'};
    await saveAction(data,task?(closed?'待办已重新打开':'待办已完成'):'日程已确认',task?{status:item.status,wake_at:item.wake_at}:null);
  });
  const needsHour=!task&&item.starts_at==null&&!!item.scheduled_date;
  $('action-date-label').textContent=task?'截止日期':needsHour?'补充具体时间':'安排日期';
  $('action-dates').hidden=needsHour;
  $('action-date-note').textContent=task?'保留原时间；未设时间的待办安排在当天 23:59。':
    (item.starts_at==null?(item.scheduled_date?'日期已确定，直接选择具体时间即可。':'先选日期，再选时间。'):'改期会保留原来的时间和时长。');
  const today=dateKey(Date.now()/1000,actionZone()),weekday=new Date(today+'T00:00:00Z').getUTCDay();
  const shortcuts=[['今天',today],['明天',plusDays(today,1)],['周末',plusDays(today,(6-weekday+7)%7)],['下周一',plusDays(monday(today),7)]];
  $('action-dates').replaceChildren(...shortcuts.map(([label,key])=>{
    const b=button('',()=>chooseActionDate(key),'date-choice'+(key===actionDate()?' selected':''));
    b.append(element('b',label),element('span',key.slice(5).replace('-','/')));return b;
  }));
  for(const id of ['action-picker','action-times','action-custom-time'])$(id).hidden=true;
  $('action-pick-date').textContent=needsHour?'调整日期':'其他日期';
  $('action-time-toggle').hidden=!actionDate()||needsHour;
  $('action-clear-date').hidden=!actionDate();
  $('action-clear-date').textContent=task?'移除截止日期':'移回待安排';
  $('action-followup').hidden=!task||closed;
  $('action-wait').textContent=item.status==='waiting'?'继续推进':'等待回复';
  $('action-cancel').hidden=closed;
  $('action-cancel').textContent=task?'取消待办':recurring?'取消整组':'取消日程';
  $('action-evidence').replaceChildren();
  if(item.evidence?.length)$('action-evidence').append(recordEvidence(item.evidence,kind+'-'+item.id));
  $('action-detail').hidden=!task;if(task)$('action-detail').href=item.detail_path;
  $('action-notes').textContent=item.notes||'';$('action-notes').hidden=!item.notes;
  $('action-error').textContent='';dialog.showModal();
  if(!task&&item.starts_at==null&&item.scheduled_date){chosenDate=item.scheduled_date;showActionTimes();}
}
function actionClocks(){
  const period=actionKind==='event'&&actionItem.starts_at==null?actionItem.time_period:'';
  return ({morning:['08:00','09:00','10:00','11:00'],noon:['12:00','12:30','13:00'],afternoon:['14:00','15:00','16:00','17:00'],evening:['18:00','19:00','20:00','21:00'],night:['22:00','23:00']})[period]||['09:00','14:00','19:00'];
}
function showActionTimes(){
  $('action-times').hidden=false;$('action-custom-time').hidden=true;
  $('action-time-label').textContent=chosenDate+' · '+(actionKind==='task'?'选择截止时间':'选择开始时间');
  $('action-time-choices').replaceChildren(...actionClocks().map(value=>button(value,()=>runAction(()=>scheduleAction(chosenDate,value)))));
  $('action-all-day').hidden=actionKind==='task'||!!actionItem.time_period;
}
async function runAction(action){
  const dialog=$('item-actions');dialog.setAttribute('aria-busy','true');
  const controls=[...dialog.querySelectorAll('button')].filter(b=>!b.disabled);controls.forEach(b=>b.disabled=true);
  try{await action();}catch(error){$('action-error').textContent=error.message;}
  finally{dialog.removeAttribute('aria-busy');controls.forEach(b=>b.disabled=false);}
}
async function saveAction(data,message,undo){
  const item=actionItem,kind=actionKind;
  await api(itemPath(item,kind),data);$('item-actions').close();await refresh();
  showToast(message,undo?async()=>{await api(itemPath(item,kind),undo);await refresh();}:null);
}
async function chooseActionDate(key){
  chosenDate=key;
  if(actionKind==='event'&&actionItem.starts_at==null){
    showActionTimes();
    $('action-times').scrollIntoView({block:'nearest',behavior:'smooth'});return;
  }
  await runAction(()=>scheduleAction(key));
}
async function scheduleAction(key,time=null){
  const item=actionItem,task=actionKind==='task',old=actionTime(),zone=actionZone();
  const oldClock=old==null?null:localValue(old,zone).slice(11,16);
  if(task){
    const due=key+'T'+(time||oldClock||'23:59');
    await saveAction({due_at:due,wake_at:due},'截止时间已调整');return;
  }
  const allDay=time==='all-day'||(time==null&&item.all_day);
  const start=key+'T'+(allDay?'00:00':time||oldClock);
  let end=null;
  if(allDay){
    const days=item.all_day&&item.ends_at!=null?Math.max(1,Math.round((Date.parse(eventRange(item)[1])-Date.parse(eventRange(item)[0]))/86400000)+1):1;
    end=plusDays(key,days)+'T00:00';
  }else if(item.ends_at!=null&&item.starts_at!=null){
    // Move wall-clock dates; the API interprets them in this item's timezone.
    const naive=Date.parse(start+'Z')+(item.ends_at-item.starts_at)*1000;
    end=new Date(naive).toISOString().slice(0,16);
  }
  await saveAction({starts_at:start,ends_at:end,all_day:allDay},'日程时间已调整');
}
function renderActionMonth(){
  const current=pickerMonth,first=current+'-01',weekday=(new Date(first+'T00:00:00Z').getUTCDay()+6)%7;
  const next=monthOffset(current,1);
  const count=Math.round((Date.parse(next+'-01')-Date.parse(first))/86400000);
  $('action-month-label').textContent=current.replace('-',' 年 ')+' 月';
  const grid=$('action-month-days');grid.replaceChildren();
  for(let i=0;i<weekday;i++)grid.append(element('span'));
  for(let day=1;day<=count;day++){
    const key=current+'-'+String(day).padStart(2,'0'),b=button(String(day),()=>chooseActionDate(key),'month-day'+(key===actionDate()?' selected':''));
    b.setAttribute('aria-label',key);grid.append(b);
  }
}
function monthOffset(month,amount){const date=new Date(month+'-01T00:00:00Z');date.setUTCMonth(date.getUTCMonth()+amount);return date.toISOString().slice(0,7);}
$('action-close').onclick=()=>$('item-actions').close();
$('action-pick-date').onclick=()=>{
  $('action-picker').hidden=!$('action-picker').hidden;
  pickerMonth=pickerMonth||(actionDate()||dateKey(Date.now()/1000,actionZone())).slice(0,7);renderActionMonth();
};
$('action-month-prev').onclick=()=>{pickerMonth=monthOffset(pickerMonth,-1);renderActionMonth();};
$('action-month-next').onclick=()=>{pickerMonth=monthOffset(pickerMonth,1);renderActionMonth();};
$('action-time-toggle').onclick=()=>{
  chosenDate=actionDate();if($('action-times').hidden)showActionTimes();else $('action-times').hidden=true;
};
for(const b of document.querySelectorAll('[data-action-time]'))b.onclick=()=>runAction(()=>scheduleAction(chosenDate,b.dataset.actionTime));
$('action-custom-toggle').onclick=()=>{
  $('action-custom-time').hidden=!$('action-custom-time').hidden;
  const current=actionTime()==null?actionClocks()[0]:localValue(actionTime(),actionZone()).slice(11,16);
  $('action-hour').value=current.slice(0,2);$('action-minute').value=current.slice(3,5);
};
for(let h=0;h<24;h++)$('action-hour').add(new Option(String(h).padStart(2,'0')+' 时',String(h).padStart(2,'0')));
for(let m=0;m<60;m++)$('action-minute').add(new Option(String(m).padStart(2,'0')+' 分',String(m).padStart(2,'0')));
$('action-custom-save').onclick=()=>runAction(()=>scheduleAction(chosenDate,$('action-hour').value+':'+$('action-minute').value));
$('action-clear-date').onclick=()=>runAction(()=>saveAction(actionKind==='task'?{due_at:null}:{starts_at:null,ends_at:null,scheduled_date:null,time_period:'',time_text:''},'已移回待安排'));
$('action-wait').onclick=()=>runAction(()=>saveAction({status:actionItem.status==='waiting'?'open':'waiting',wake_at:'1d'},'跟进状态已更新'));
$('action-later').onclick=()=>runAction(()=>saveAction({wake_at:'1h'},'将在 1 小时后跟进'));
$('action-cancel').onclick=()=>runAction(()=>saveAction({status:'cancelled'},actionKind==='task'?'待办已取消':'日程已取消'));
$('action-delete').onclick=()=>runAction(()=>deleteItem(actionItem,actionKind));
$('action-edit').onclick=()=>{
  const item=actionItem,kind=actionKind;$('item-actions').close();
  if(kind==='task')editTask(item);else editEvent(item);
};
$('item-actions').addEventListener('click',event=>{if(event.target===$('item-actions')){const r=event.target.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)event.target.close();}});
