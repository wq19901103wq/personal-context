'use strict';
function renderDevices(){
  const box=$('devices');box.replaceChildren();
  if(!state.devices?.length){empty(box,'还没有接入设备。','添加设备后，将语音转写发送到接入地址。');return;}
  for(const device of state.devices){
    const card=element('article',undefined,'task');
    card.append(element('h3',device.name),element('p',(device.enabled?'接收已启用':'已停止接收')+' · 已收到 '+device.records+' 条 · 待整理 '+device.pending+' 条','muted'));
    if(device.last_received_at)card.append(element('p','最近收到 '+stamp(device.last_received_at),'muted'));
    card.append(element('p',device.self_speaker_id?'本人说话者 ID：'+device.self_speaker_id:'说话者身份默认未确认','muted'));
    const actions=element('div',undefined,'actions');
    if(device.pending)actions.append(button('重试未完成的整理',async()=>{await api('/api/devices/'+device.id+'/retry',{});toast('已安排继续整理原文');await refresh();}));
    if(device.enabled)actions.append(button('停止接收',async()=>{await api('/api/devices/'+device.id+'/revoke',{});toast('已停用接入凭据，原文与已整理内容保留');await refresh();},'small quiet'));
    card.append(actions);box.append(card);
  }
}
$('device-open').onclick=()=>{$('device-form').reset();$('device-error').textContent='';$('device-dialog').showModal();};
$('device-form').onsubmit=async event=>{
  event.preventDefault();event.submitter.disabled=true;
  try{
    const device=await api('/api/devices',{name:$('device-name').value,self_speaker_id:$('device-speaker').value});
    const sample={event_id:'填写设备产生的稳定事件 ID',text:'填写最终语音转写',occurred_at:new Date().toISOString()};
    if($('device-speaker').value)sample.speaker_id=$('device-speaker').value;
    $('device-config').value='POST '+device.url+'\nAuthorization: Bearer '+device.token+'\nContent-Type: application/json\n\n'+JSON.stringify(sample,null,2);
    $('device-dialog').close();$('device-pairing').showModal();await refresh();
  }catch(error){$('device-error').textContent=error.message;}
  finally{event.submitter.disabled=false;}
};
$('device-pairing').addEventListener('close',()=>{$('device-config').value='';});
$('device-copy').onclick=async()=>{try{await navigator.clipboard.writeText($('device-config').value);toast('接入信息已复制');}catch(error){$('device-config').select();toast('请复制已选中的接入信息');}};
