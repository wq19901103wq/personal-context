let questionParent=null, questionSignature='', questionPoll=null, questionLoading=false;
function followQuestion(item){
  questionParent=item.id;$('question-followup').hidden=false;
  $('question-followup').querySelector('span').textContent='继续问：'+item.question;
  $('question-input').focus();$('question-form').scrollIntoView({block:'center',behavior:'smooth'});
}
function resetQuestion(){questionParent=null;$('question-followup').hidden=true;}
function renderQuestions(items){
  const signature=JSON.stringify(items);if(signature===questionSignature)return;questionSignature=signature;
  const host=$('question-history');host.replaceChildren();
  if(!items.length){host.append(element('p','还没有提问，点上面的例子试试。','muted'));return;}
  const byId=new Map(items.map(item=>[item.id,item]));
  for(const item of items){
    const row=element('article',undefined,'question-item');row.append(element('h3',item.question));
    if(item.parent_id&&byId.has(item.parent_id))row.append(element('p','接着问：'+byId.get(item.parent_id).question,'muted'));
    const pending=item.status==='queued'||item.status==='running';
    const answer=element('div',undefined,pending?'muted':'question-answer');
    if(item.status==='completed'){
      String(item.answer??'').split(/\*\*([^*\n]+)\*\*/g).forEach((part,index)=>answer.append(index%2?element('strong',part):document.createTextNode(part)));
    }else answer.textContent=item.status==='failed'?item.error:item.status==='running'?'正在查找相关记忆…':'已收到，稍后为你查找…';
    row.append(answer);
    const actions=element('div',undefined,'actions');
    if(item.status==='completed')actions.append(button('继续问',()=>followQuestion(item),'small quiet'));
    if(item.status==='failed')actions.append(button('重试',async()=>{try{await api('/api/questions/'+item.id+'/retry',{});await refreshQuestions();}catch(error){$('question-error').textContent=error.message;}},'small'));
    const date=element('time',stamp(item.created_at));date.dateTime=new Date(item.created_at*1000).toISOString();actions.append(date);row.append(actions);host.append(row);
  }
}
async function refreshQuestions(){
  if(questionLoading)return;questionLoading=true;clearTimeout(questionPoll);
  let active=false;
  try{const items=await api('/api/questions');renderQuestions(items);active=items.some(i=>i.status==='queued'||i.status==='running');$('question-error').textContent='';}
  catch(error){$('question-error').textContent='暂时无法读取问答：'+error.message;}
  finally{questionLoading=false;questionPoll=setTimeout(refreshQuestions,active?2500:15000);}
}
$('question-new').onclick=resetQuestion;
$('question-form').onsubmit=async event=>{
  event.preventDefault();const question=$('question-input').value.trim();if(!question)return;
  $('question-submit').disabled=true;
  try{await api('/api/questions',{question,parent_id:questionParent});$('question-input').value='';resetQuestion();await refreshQuestions();}
  catch(error){$('question-error').textContent=error.message;}
  finally{$('question-submit').disabled=false;}
};
document.querySelectorAll('[data-question]').forEach(node=>node.onclick=()=>{resetQuestion();$('question-input').value=node.dataset.question;$('question-form').requestSubmit();});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshQuestions();});
refreshQuestions();
