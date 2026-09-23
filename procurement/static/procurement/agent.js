(() => {
 const form=document.getElementById('agent-form'), chat=document.getElementById('agent-chat');
 const input=document.getElementById('agent-message'), supplier=document.getElementById('agent-supplier');
 const send=document.getElementById('agent-send'), reset=document.getElementById('agent-reset'), status=document.getElementById('agent-status');
 const csrf=form.querySelector('[name=csrfmiddlewaretoken]').value;
 let busy=false;
 function element(tag,text,className){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(className)e.className=className;return e;}
 function message(role,text){document.getElementById('agent-empty')?.remove();const box=element('article',undefined,'agent-turn '+role);box.append(element('div',role==='user'?'Вы':role==='error'?'Запрос не выполнен':'AI-агент','agent-role'),element('div',text,'agent-message'));chat.append(box);return box;}
 const remembered=JSON.parse(document.getElementById('agent-history').textContent);remembered.forEach(m=>message(m.role,m.content));
 function budget(b){if(b)document.getElementById('agent-budget').textContent=`Учтено $${b.accounted_usd.toFixed(4)} из $${b.limit_usd.toFixed(2)}`;}
 function controls(on){busy=on;send.disabled=on;reset.disabled=on;supplier.disabled=on;document.querySelectorAll('.starter').forEach(b=>b.disabled=on);}
 async function post(url,body){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':csrf},body:JSON.stringify(body)});let data;try{data=await r.json();}catch{throw new Error('Сервер не вернул ответ. Обновите страницу и проверьте вход.');}return {r,data};}
 async function clear(){const {r,data}=await post(form.dataset.reset,{});if(!r.ok)throw new Error(data.error||'Не удалось начать новый диалог.');chat.replaceChildren();input.value='';status.textContent='Новый диалог';}
 reset.addEventListener('click',async()=>{if(busy)return;try{await clear();}catch(e){status.textContent=e.message;}});
 supplier.addEventListener('change',async()=>{try{await clear();}catch(e){status.textContent=e.message;}});
 document.querySelectorAll('.starter').forEach(button=>button.addEventListener('click',()=>{if(busy||input.disabled)return;input.value=button.dataset.question;input.focus();}));
 form.addEventListener('submit',async event=>{
  event.preventDefault();if(busy)return;const question=input.value.trim();if(!question)return;
  message('user',question);input.value='';controls(true);status.textContent='Агент анализирует данные… Обычно 10–40 секунд.';
  try{
   const {r,data}=await post(form.dataset.url,{message:question,supplier:supplier.value});budget(data.budget);
   if(!r.ok)throw new Error(data.error||'Запрос не выполнен.');
   const box=message('assistant',data.answer);
   data.trace.filter(t=>t.name==='simulate_product'&&t.result.scenario).forEach(t=>{
    const r=t.result, facts=element('div',undefined,'alert alert-primary mt-3 mb-2');
    facts.append(element('strong','Числа из расчётного алгоритма'),element('div',`Код ${r.scenario.code}: стандартный заказ ${r.base.recommended_quantity}, сценарий ${r.scenario.recommended_quantity}. Изменение относительно стандарта: ${r.quantity_delta>0?'+':''}${r.quantity_delta} ${r.scenario.unit}.`));
    if(r.previous_scenario)facts.append(element('div',`Предыдущий сценарий: ${r.previous_scenario.quantity}. Изменение относительно него: ${r.quantity_delta_from_previous>0?'+':''}${r.quantity_delta_from_previous} ${r.scenario.unit}.`));
    box.append(facts);
   });
   box.append(element('div',`${data.model} · данные на ${data.data_date} · ${data.usage.input_tokens+data.usage.output_tokens} токенов · ${data.usage.cost_usd===null?'расход уточняется':'$'+data.usage.cost_usd.toFixed(5)}`,'small text-muted mt-3'));
   if(data.sources.length){const sources=element('div',undefined,'mt-2');sources.append(element('div','Карточки товаров','small fw-semibold'));data.sources.forEach(s=>{if(!s.url.startsWith('/sku/'))return;const a=element('a',`${s.code} · ${s.name}`,'agent-source');a.href=s.url;a.target='_blank';a.rel='noopener';sources.append(a);});box.append(sources);}
   if(data.trace.length){const details=element('details',undefined,'agent-tools mt-3');details.append(element('summary',`Проверить действия агента (${data.trace.length})`));data.trace.forEach(t=>{details.append(element('h3',t.label,'h6 mt-3'),element('pre',JSON.stringify({parameters:t.arguments,result:t.result},null,2)));});box.append(details);}
   status.textContent='Готово. Можно задать уточняющий вопрос.';
  }catch(error){message('error',error.message);status.textContent='Основной расчёт заказов продолжает работать.';input.value=question;}
  finally{controls(false);}
 });
})();
