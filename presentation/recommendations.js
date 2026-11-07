'use strict';

function renderInsights(){
  const config=state.personalization||{};
  const save=async data=>{await api('/api/personalization',data);await refresh();};
  $('recommendation-toggle').textContent=config.enabled?'暂停推荐':'开启推荐';
  $('recommendation-toggle').onclick=async()=>{try{await save({enabled:!config.enabled});}catch(e){toast(e.message);}};
  const location=config.current_location, fresh=location&&Date.now()/1000-location.observed_at<12*3600;
  $('location-summary').textContent=fresh?'已使用你提供的大致位置，12 小时内有效。':'未获取实时位置；可参考行程目的地和有日期的历史常用区域。';
  $('clear-location').hidden=!location;$('clear-location').onclick=async()=>{try{await save({location:null});}catch(e){toast(e.message);}};
  $('use-location').onclick=()=>{
    if(!navigator.geolocation){toast('当前浏览器不支持定位');return;}
    const b=$('use-location');b.disabled=true;b.textContent='正在获取位置…';
    navigator.geolocation.getCurrentPosition(async position=>{
      try{await save({location:{latitude:position.coords.latitude,longitude:position.coords.longitude}});toast('位置已更新，会在合适时机推荐');}
      catch(e){toast(e.message);}finally{b.disabled=false;b.textContent='⌖ 用当前位置推荐';}
    },()=>{b.disabled=false;b.textContent='⌖ 用当前位置推荐';toast('未获得位置，请允许浏览器定位后重试');},{enableHighAccuracy:false,timeout:15000,maximumAge:300000});
  };
  const box=$('insights');box.replaceChildren();
  const items=(state.insights||[]).filter(i=>i.feedback!=='not_interested'), stale=i=>['expired','superseded','dismissed'].includes(i.notification_status);
  $('insight-count').textContent=items.filter(i=>!i.read_at&&!stale(i)).length+' 条未读';
  if(!items.length){box.append(element('p',config.enabled?'临近出行、明天或当天日历有空档时，助理会结合兴趣寻找展览和餐厅。没有合适结果就不打扰。':'推荐已暂停。你可以随时重新开启。','discovery-empty'));return;}
  const labels={pending:'合适时段微信推送',sending:'正在推送',delivered:'微信接口已接受',disabled:'保存在这里',failed:'微信推送失败',retry:'推送未完成',unknown:'推送结果未知，请查看微信',expired:'机会已过期',superseded:'安排或偏好已变化'};
  for(const i of items){
    const card=element('article',undefined,'discovery-card'+(stale(i)?' is-stale':''));
    card.append(element('p',stamp(i.created_at)+' · '+(labels[i.notification_status]||i.notification_status),'muted'),element('h3',i.title),element('div',i.body,'insight-body'),element('p','为什么推荐：'+i.reason,'notes'));
    if(i.notification_error)card.append(element('p',i.notification_error,'error'));
    const actions=element('div',undefined,'actions');
    if(/^https?:\/\//i.test(i.url||'')){const a=element('a',i.discovery?.category==='restaurant'?'查看门店 ↗':'查看详情 ↗');a.href=i.url;a.target='_blank';a.rel='noopener noreferrer';actions.append(a);}
    if(!i.read_at)actions.append(button('知道了',async()=>{await api('/api/insights/'+i.id+'/read',{});await refresh();},'small quiet'));
    actions.append(button('不感兴趣',async()=>{await api('/api/insights/'+i.id+'/dismiss',{});await refresh();toast('以后不再推荐这个去处');},'small quiet'));
    card.append(actions);box.append(card);
  }
}
