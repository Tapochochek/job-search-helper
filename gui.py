#!/usr/bin/env python3
"""Локальный интерфейс hh_autoapply в стиле hh.ru.  Запуск: python gui.py  ->  http://127.0.0.1:8765"""
import json, os, re, subprocess, sys, threading, urllib.error, urllib.parse, urllib.request, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from filters import build_search_url, parse_search_url
from report import load_events
from tg_bot import Bot

ROOT, PORT = Path(__file__).parent, 8765
SEARCH_URL_RE = re.compile(r"^https://([a-z0-9-]+\.)?hh\.ru/search/vacancy\S*$")
URL_RE = re.compile(r"^https://([a-z0-9-]+\.)?hh\.ru/vacancy/\d+\S*$")
ENV = ROOT / ".env"
TOKEN_RE, CHAT_RE = re.compile(r"^\d{6,}:[\w-]{30,}$"), re.compile(r"^(-?\d{4,}|@\w{4,})$")
MODEL_RE, KEY_RE = re.compile(r"^[\w.:/@+-]{1,80}$"), re.compile(r"^[\w.-]{10,300}$")
PROVIDERS = {  # провайдер -> (переменная модели, переменная ключа)
    "ollama": ("OLLAMA_MODEL", None), "openai": ("OPENAI_MODEL", "OPENAI_API_KEY"),
    "anthropic": ("ANTHROPIC_MODEL", "ANTHROPIC_API_KEY"), "openrouter": ("OPENROUTER_MODEL", "OPENROUTER_API_KEY"),
    "deepseek": ("DEEPSEEK_MODEL", "DEEPSEEK_API_KEY"),
}
PRESETS, EVENTS = ROOT / "data" / "searches.json", ROOT / "data" / "events.jsonl"
NAME_RE = re.compile(r"^[\w .#+/()-]{1,40}$")
BOT_ON = False
LOG: list[str] = []
PROC: subprocess.Popen | None = None

PAGE = r"""<!doctype html><html lang=ru><meta charset=utf-8><title>hh автоотклики</title>
<meta name=viewport content="width=device-width,initial-scale=1"><style>
:root{--red:#d6001c;--blue:#0070ff;--bg:#f1f2f5;--card:#fff;--ink:#1b1c20;--mut:#6b7280;--line:#dfe2e8;--chip:#eef0f4;--ok:#1a9d55}
@media(prefers-color-scheme:dark){:root{--bg:#131417;--card:#1e2024;--ink:#f1f2f4;--mut:#9aa0aa;--line:#33363d;--chip:#2a2d34}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
header{background:#20232a;color:#fff;padding:12px 24px;display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:5}
.logo{width:34px;height:34px;border-radius:50%;background:var(--red);display:grid;place-items:center;font-weight:700}
header b{font-size:17px}#st{margin-left:auto;font-size:13px;padding:5px 12px;border-radius:99px;background:#ffffff1f}
#st.on{background:var(--blue)}
.wrap{max-width:1180px;margin:20px auto;padding:0 16px;display:grid;grid-template-columns:370px 1fr;gap:16px;align-items:start}
@media(max-width:980px){.wrap{grid-template-columns:1fr}}
.card{background:var(--card);border-radius:16px;padding:18px 20px}.col{display:grid;gap:16px}
h2{margin:0 0 12px;font-size:18px}h3{margin:16px 0 8px;font-size:13px;font-weight:600;color:var(--mut)}
input,select,textarea{width:100%;border:1px solid var(--line);border-radius:10px;padding:10px 12px;font:inherit;background:transparent;color:inherit}
input:focus,select:focus,textarea:focus{outline:2px solid var(--blue);border-color:transparent}
textarea{height:140px;resize:vertical}select option{color:#000}
.chips{display:flex;flex-wrap:wrap;gap:7px}.chips label{margin:0;position:relative}
.chips input{position:absolute;opacity:0;width:0;height:0}
.chips span{display:inline-block;padding:7px 13px;border-radius:99px;background:var(--chip);cursor:pointer;font-size:14px;user-select:none}
.chips input:checked+span{background:var(--blue);color:#fff}.chips input:focus-visible+span{outline:2px solid var(--blue);outline-offset:2px}
.chip{display:inline-flex;gap:6px;align-items:center;padding:6px 8px 6px 12px;border-radius:99px;background:var(--chip);font-size:14px}
.chip button{all:unset;cursor:pointer;color:var(--mut);padding:0 4px}
.sug{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.sug button,.link{all:unset;cursor:pointer;color:var(--blue);font-size:14px;padding:2px 6px;border-radius:6px}.sug button:hover{background:var(--chip)}
.row{display:flex;flex-wrap:wrap;align-items:center;gap:10px}.row.sp{margin-top:14px}.row>*{width:auto}
label.ck{display:flex;gap:8px;align-items:center;cursor:pointer;margin:0 14px 0 0}label.ck input{width:auto}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
button.b{border:0;border-radius:12px;padding:11px 20px;font:600 15px inherit;cursor:pointer;color:#fff;background:var(--blue)}
button.b.sec{background:transparent;color:var(--blue);border:1px solid var(--blue)}button.b.stop{background:var(--red)}
button.b:disabled{opacity:.45;cursor:default}
.tabs{display:flex;gap:6px;margin-bottom:14px}.tabs button{all:unset;cursor:pointer;padding:8px 16px;border-radius:99px;background:var(--chip);font-weight:600;font-size:14px}
.tabs button.on{background:var(--ink);color:var(--bg)}.hide{display:none!important}
#built{font:12px ui-monospace,monospace;color:var(--mut)}.err{color:var(--red);font-size:13px;min-height:18px}
#log{background:#101114;color:#d8dbe0;border-radius:12px;padding:14px;height:360px;overflow:auto;white-space:pre-wrap;font:13px/1.5 ui-monospace,monospace}
.w{color:#ffb454}.e{color:#ff6b6b}.ok{color:#5fd68a}
details{border-top:1px solid var(--line);padding-top:12px;margin-top:12px}summary{cursor:pointer;font-weight:600}
details input,details select{margin-top:8px}.hint{color:var(--mut);font-size:13px;margin-top:6px}
.msg{font-size:13px}
</style>
<header><div class=logo>hh</div><b>Автоотклики</b><span id=st>Готово</span></header>
<div class=wrap>
<aside class=col><section class=card><h2>Фильтры</h2>
<input id=text placeholder="Должность, ключевые слова" autocomplete=off>
<h3>Искать только</h3><div class=chips>
<label><input type=checkbox data-k=search_field value=name><span>В названии вакансии</span></label>
<label><input type=checkbox data-k=search_field value=company_name><span>В названии компании</span></label>
<label><input type=checkbox data-k=search_field value=description><span>В описании вакансии</span></label></div>
<h3>Регион</h3><div id=areas class=chips></div>
<input id=areaq placeholder="Начни вводить город…" autocomplete=off style="margin-top:8px">
<div id=areasug class=sug></div>
<div class=sug id=areapre><button data-id=113>+ Россия</button><button data-id=1>+ Москва</button><button data-id=2>+ Санкт-Петербург</button><button data-id=3>+ Екатеринбург</button></div>
<h3>Формат работы</h3><div class=chips>
<label><input type=checkbox data-k=work_format value=REMOTE><span>Удалённо</span></label>
<label><input type=checkbox data-k=work_format value=HYBRID><span>Гибрид</span></label>
<label><input type=checkbox data-k=work_format value=ON_SITE><span>На месте работодателя</span></label>
<label><input type=checkbox data-k=work_format value=FIELD_WORK><span>Разъездной</span></label></div>
<h3>Опыт работы</h3><div class=chips>
<label><input type=checkbox data-k=experience value=noExperience><span>Нет опыта</span></label>
<label><input type=checkbox data-k=experience value=between1And3><span>От 1 до 3 лет</span></label>
<label><input type=checkbox data-k=experience value=between3And6><span>От 3 до 6 лет</span></label>
<label><input type=checkbox data-k=experience value=moreThan6><span>Более 6 лет</span></label></div>
<h3>График работы</h3><div class=chips>
<label><input type=checkbox data-k=schedule value=FIVE_ON_TWO_OFF><span>5/2</span></label>
<label><input type=checkbox data-k=schedule value=TWO_ON_TWO_OFF><span>2/2</span></label>
<label><input type=checkbox data-k=schedule value=SIX_ON_ONE_OFF><span>6/1</span></label></div>
<h3>Доход и свежесть</h3><div class=grid2><input id=salary type=number min=0 step=1000 placeholder="Зарплата от, ₽"><select id=period>
<option value="">За всё время</option><option value=1>За сутки</option><option value=3>За 3 дня</option><option value=7>За неделю</option><option value=30>За месяц</option></select></div>
<label class=ck style="margin-top:10px"><input type=checkbox id=ows> Только с указанной зарплатой</label>
<h3>Сортировка</h3><select id=order><option value=publication_time>По дате</option><option value=relevance>По соответствию</option><option value=salary_desc>По убыванию зарплаты</option><option value=salary_asc>По возрастанию зарплаты</option></select>
<h3>Уже настроен фильтр на hh.ru?</h3><input id=paste placeholder="Вставь ссылку поиска hh.ru">
<div class="row sp"><button class="b sec" id=parse>Разобрать ссылку</button><span id=pmsg class=msg></span></div>
</section></aside>
<main class=col>
<section class=card><div class=tabs><button data-m=search class=on>Автопоиск</button><button data-m=urls>Конкретные вакансии</button></div>
<div id=m_search><div class=hint style="margin:0 0 8px">Ссылка поиска, которую откроет скрипт (собрана из фильтров слева):</div>
<input id=built readonly><div class=row style="margin-top:6px"><a id=open class=link target=_blank rel=noopener href="#">Открыть на hh.ru и сверить ↗</a><span id=berr class=err></span></div>
<div class="row sp"><select id=presets style="flex:1;min-width:170px"><option value="">Сохранённые поиски…</option></select><button class="b sec" id=pload>Загрузить</button><button class="b sec" id=pdel>Удалить</button></div>
<div class="row sp"><input id=pname placeholder="Название (например: C# удалёнка)" style="flex:1;min-width:170px"><button class="b sec" id=psave>Сохранить поиск</button><span id=pmsg2 class=msg></span></div>
<div class=hint>Сохранённые поиски запускаются кнопками из Telegram-бота.</div>
<div class="row sp"><label>Страниц<input id=pg type=number value=3 min=1 max=20 style="width:80px;margin-left:8px"></label>
<label>Макс. вакансий<input id=lim type=number value=20 min=1 max=100 style="width:90px;margin-left:8px"></label></div></div>
<div id=m_urls class=hide><textarea id=u placeholder="Ссылки на вакансии hh.ru, по одной в строке"></textarea></div>
<div class="row sp"><label class=ck><input type=checkbox id=sub> Отправлять отклик</label><label class=ck><input type=checkbox id=dbg> Отладка (дамп в ./debug)</label></div>
<div class="row sp"><button class=b id=run>Запустить</button><button class="b sec" id=sync>Обновить резюме</button><button class="b stop" id=stop disabled>Остановить</button></div>
<div class=hint>Без галочки «Отправлять отклик» скрипт только заполняет формы — отправляешь ты сам.</div></section>
<section class=card><h2>Журнал</h2><div id=log></div></section>
<section class=card><h2>Настройки</h2>
<details open><summary>Нейросеть</summary>
<div class=grid2><select id=prov><option>ollama</option><option>openai</option><option>anthropic</option><option>openrouter</option><option>deepseek</option></select>
<input id=model placeholder="Модель (например gemma3:12b)"></div>
<input id=key type=password placeholder="API-ключ (для облачных провайдеров)" autocomplete=off>
<div class=grid2><select id=gender><option value=neutral>Род: без глаголов прошедшего времени</option><option value=male>Я — мужчина</option><option value=female>Я — женщина</option></select>
<input id=minfit type=number min=0 max=10 placeholder="Мин. балл соответствия (0–10)"></div>
<div class=hint>Мин. балл: вакансии, где лучшее резюме набрало меньше, пропускаются (по умолчанию 5).</div>
<div class="row sp"><button class=b id=saveai>Сохранить</button><span id=amsg class=msg></span></div></details>
<details><summary>Telegram</summary>
<div class=hint id=botst style="margin:8px 0 0"></div>
<input id=tok type=password placeholder="Токен бота (от @BotFather)" autocomplete=off>
<input id=chat placeholder="ID чата (число или @username)" autocomplete=off>
<div class="row sp"><button class=b id=save>Сохранить</button><button class="b sec" id=test>Проверить связь</button><span id=tmsg class=msg></span></div></details>
</section></main></div>
<script>
const $=i=>document.getElementById(i),Q=s=>[...document.querySelectorAll(s)];
const post=(p,b)=>fetch(p,{method:'POST',headers:{'X-GUI':'1'},body:JSON.stringify(b||{})}).then(r=>r.json());
const KEYS=['search_field','experience','work_format','schedule'],PRE={113:'Россия',1:'Москва',2:'Санкт-Петербург',3:'Екатеринбург'};
let areas=[],extra=[],host='hh.ru',mode='search',n=0,busy=false,timer;
function filters(){const f={text:$('text').value,area:areas.map(a=>a.id),extra,host,salary:$('salary').value,only_with_salary:$('ows').checked,period:$('period').value,order_by:$('order').value};
 for(const k of KEYS)f[k]=Q(`input[data-k=${k}]:checked`).map(e=>e.value);return f}
function drawAreas(){const box=$('areas');box.textContent='';areas.forEach((a,i)=>{const c=document.createElement('span');c.className='chip';c.textContent=a.name;
 const x=document.createElement('button');x.textContent='✕';x.title='Убрать';x.onclick=()=>{areas.splice(i,1);drawAreas();changed()};c.append(x);box.append(c)})}
function addArea(id,name){id=String(id);if(!areas.some(a=>a.id===id))areas.push({id,name:name||PRE[id]||'Регион '+id});drawAreas();changed()}
function apply(f){$('text').value=f.text||'';areas=(f.area||[]).map(id=>({id:String(id),name:PRE[id]||'Регион '+id}));extra=f.extra||[];host=f.host||'hh.ru';
 for(const k of KEYS)Q(`input[data-k=${k}]`).forEach(e=>e.checked=(f[k]||[]).includes(e.value));
 $('salary').value=f.salary||'';$('ows').checked=!!f.only_with_salary;$('period').value=f.period||'';$('order').value=f.order_by||'publication_time';drawAreas()}
function save(){try{localStorage.setItem('hhf',JSON.stringify({f:filters(),areas,mode,pg:$('pg').value,lim:$('lim').value}))}catch(e){}}
function changed(){clearTimeout(timer);timer=setTimeout(()=>post('/build',{filters:filters()}).then(r=>{$('built').value=r.url||'';$('open').href=r.url||'#';$('berr').textContent=r.error||'';save()}),250)}
function setMode(m){mode=m;Q('.tabs button').forEach(b=>b.classList.toggle('on',b.dataset.m===m));$('m_search').classList.toggle('hide',m!=='search');$('m_urls').classList.toggle('hide',m!=='urls');save()}
Q('.tabs button').forEach(b=>b.onclick=()=>setMode(b.dataset.m));
Q('.card input:not(#areaq):not(#paste):not(#built),.card select').forEach(e=>{if(e.closest('aside'))e.addEventListener('input',changed)});
Q('#areapre button').forEach(b=>b.onclick=()=>addArea(b.dataset.id));
let at;$('areaq').oninput=()=>{clearTimeout(at);const v=$('areaq').value.trim();if(v.length<2){$('areasug').textContent='';return}
 at=setTimeout(()=>fetch('/areas?text='+encodeURIComponent(v)).then(r=>r.json()).then(r=>{const s=$('areasug');s.textContent='';
  (r.items||[]).forEach(it=>{const b=document.createElement('button');b.textContent='+ '+it.text;b.onclick=()=>{addArea(it.id,it.text.split(',')[0]);$('areaq').value='';s.textContent=''};s.append(b)});
  if(!(r.items||[]).length){s.textContent=r.error||'Ничего не найдено'}}),300)};
$('parse').onclick=()=>post('/parse',{url:$('paste').value}).then(r=>{if(r.error){$('pmsg').textContent=r.error;$('pmsg').style.color='var(--red)';return}
 apply(r.filters);$('pmsg').textContent='Фильтры заполнены';$('pmsg').style.color='var(--ok)';changed()});
let PR={};
function loadPresets(){fetch('/presets').then(r=>r.json()).then(r=>{PR=r;const s=$('presets');s.length=1;Object.keys(r).sort().forEach(k=>{const o=document.createElement('option');o.value=o.textContent=k;s.append(o)})})}
$('pload').onclick=()=>{const p=PR[$('presets').value];if(!p)return;apply(p.filters);$('pg').value=p.pages;$('lim').value=p.limit;changed()};
$('pdel').onclick=()=>{const k=$('presets').value;if(k)post('/preset_del',{name:k}).then(loadPresets)};
$('psave').onclick=()=>post('/preset_save',{name:$('pname').value,filters:filters(),pages:+$('pg').value,limit:+$('lim').value}).then(r=>{
 $('pmsg2').textContent=r.msg;$('pmsg2').style.color=r.ok?'var(--ok)':'var(--red)';if(r.ok)loadPresets()});
loadPresets();
function go(b){post('/run',b).then(r=>{if(r.error)add('! '+r.error)})}
const opts=()=>({submit:$('sub').checked,debug:$('dbg').checked});
$('run').onclick=()=>go(mode==='search'?{mode,filters:filters(),pages:+$('pg').value,limit:+$('lim').value,...opts()}:{mode,urls:$('u').value,...opts()});
$('sync').onclick=()=>go({mode:'sync'});$('stop').onclick=()=>post('/stop');
const tg=p=>{$('tmsg').textContent='…';post(p,{token:$('tok').value,chat:$('chat').value}).then(r=>{$('tmsg').textContent=r.msg;$('tmsg').style.color=r.ok?'var(--ok)':'var(--red)';
 if(r.ok&&p=='/settings'&&$('tok').value){$('tok').value='';$('tok').placeholder='•••• токен сохранён'}})};
$('save').onclick=()=>tg('/settings');$('test').onclick=()=>tg('/tg_test');
let S={models:{},keys:{}};
function showProv(){const p=$('prov').value;$('model').value=S.models[p]||'';$('key').placeholder=p==='ollama'?'Ключ не нужен (локальная модель)':S.keys[p]?'•••• ключ сохранён':'API-ключ';$('key').disabled=p==='ollama'}
$('prov').onchange=showProv;
$('saveai').onclick=()=>{$('amsg').textContent='…';post('/ai',{provider:$('prov').value,model:$('model').value,key:$('key').value,gender:$('gender').value,min_fit:$('minfit').value}).then(r=>{
 $('amsg').textContent=r.msg;$('amsg').style.color=r.ok?'var(--ok)':'var(--red)';if(r.ok){S.models[$('prov').value]=$('model').value.trim();if($('key').value){S.keys[$('prov').value]=true;$('key').value=''}showProv()}})};
fetch('/settings').then(r=>r.json()).then(r=>{$('chat').value=r.chat;if(r.token_set)$('tok').placeholder='•••• токен сохранён';
 S=r;$('botst').textContent=r.bot?'🤖 Бот слушает команды, пока открыт gui.py. Напиши ему /start.':'Бот выключен: сохрани токен и числовой ID чата, затем перезапусти gui.py.';$('prov').value=r.provider;$('gender').value=r.gender;$('minfit').value=r.min_fit;showProv()});
function add(t){const d=document.createElement('div');d.textContent=t;
 d.className=/ERROR|Traceback|Error/.test(t)?'e':/WARNING|^!/.test(t)?'w':/Done|Saved|submitted/.test(t)?'ok':'';
 const l=$('log'),b=l.scrollTop+l.clientHeight>=l.scrollHeight-30;l.append(d);if(b)l.scrollTop=l.scrollHeight}
setInterval(()=>fetch('/log?n='+n).then(r=>r.json()).then(r=>{r.lines.forEach(add);n=r.n;busy=r.busy;
 for(const i of['run','sync'])$(i).disabled=busy;$('stop').disabled=!busy;$('st').textContent=busy?'Работает…':'Готово';$('st').className=busy?'on':''}),900);
try{const s=JSON.parse(localStorage.getItem('hhf')||'null');if(s){apply(s.f);if(s.areas)areas=s.areas,drawAreas();$('pg').value=s.pg||3;$('lim').value=s.lim||20;setMode(s.mode||'search')}}catch(e){}
changed();
</script>"""


def _reader(p: subprocess.Popen) -> None:
    for line in p.stdout:
        LOG.append(line.rstrip())
    LOG.append(f"— процесс завершён (код {p.wait()}) —")


def read_env() -> dict:
    out = {}
    if ENV.exists():
        for ln in ENV.read_text(encoding="utf-8").splitlines():
            k, sep, v = ln.partition("=")
            if sep and not ln.lstrip().startswith("#"):
                out[k.strip()] = v.split("  #")[0].strip().strip("\"'")  # без хвостового комментария
    return out


def write_env(updates: dict) -> None:
    lines, done = (ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []), set()
    for i, ln in enumerate(lines):
        k = ln.partition("=")[0].strip()
        if k in updates and not ln.lstrip().startswith("#"):
            lines[i], _ = f"{k}={updates[k]}", done.add(k)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in done]
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ENV.chmod(0o600)


def tg_settings(body: dict, send: bool) -> dict:
    saved = read_env()
    token = str(body.get("token", "")).strip() or saved.get("TELEGRAM_BOT_TOKEN", "")
    chat = str(body.get("chat", "")).strip()
    if not TOKEN_RE.match(token):
        return {"ok": False, "msg": "Токен выглядит неверно (формат 123456:AA…)"}
    if not CHAT_RE.match(chat):
        return {"ok": False, "msg": "ID чата: число (для групп с минусом) или @username"}
    if not send:
        write_env({"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat})
        return {"ok": True, "msg": "Сохранено (применится при следующем запуске)"}
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"chat_id": chat, "text": "✅ hh автоотклики: проверка связи прошла"}).encode())
    try:
        urllib.request.urlopen(req, timeout=20)
        return {"ok": True, "msg": "Сообщение отправлено — проверь Telegram"}
    except urllib.error.HTTPError as e:  # Telegram объясняет причину в теле ответа
        try:
            why = json.loads(e.read()).get("description", e.code)
        except Exception:
            why = e.code
        hint = " — напиши боту /start и проверь ID" if "chat not found" in str(why) else ""
        return {"ok": False, "msg": f"Telegram: {why}{hint}"}
    except Exception as e:  # текст исключения не выводим: в нём может быть токен
        return {"ok": False, "msg": f"Нет связи с Telegram ({type(e).__name__})"}


def ai_settings(body: dict) -> dict:
    prov = str(body.get("provider", ""))
    if prov not in PROVIDERS:
        return {"ok": False, "msg": "Неизвестный провайдер"}
    model, key = str(body.get("model", "")).strip(), str(body.get("key", "")).strip()
    gender, fit = str(body.get("gender", "neutral")), str(body.get("min_fit", "")).strip()
    if model and not MODEL_RE.match(model):
        return {"ok": False, "msg": "Название модели: буквы, цифры и . : / - _"}
    if key and not KEY_RE.match(key):
        return {"ok": False, "msg": "Ключ выглядит неверно (лишние символы или слишком короткий)"}
    if gender not in ("neutral", "male", "female") or (fit and not (fit.isdigit() and 0 <= int(fit) <= 10)):
        return {"ok": False, "msg": "Балл — число от 0 до 10"}
    upd = {"LLM_PROVIDER": prov, "CANDIDATE_GENDER": gender}
    if model:
        upd[PROVIDERS[prov][0]] = model
    if key and PROVIDERS[prov][1]:
        upd[PROVIDERS[prov][1]] = key
    if fit:
        upd["MIN_FIT"] = fit
    write_env(upd)
    return {"ok": True, "msg": "Сохранено (применится при следующем запуске)"}


def get_settings() -> dict:
    env = read_env()
    return {
        "chat": env.get("TELEGRAM_CHAT_ID", "").replace("PUT_YOUR_TELEGRAM_CHAT_ID_HERE", ""),
        "token_set": bool(TOKEN_RE.match(env.get("TELEGRAM_BOT_TOKEN", ""))),
        "provider": env.get("LLM_PROVIDER", "ollama") if env.get("LLM_PROVIDER", "ollama") in PROVIDERS else "ollama",
        "models": {p: env.get(m, "") for p, (m, _) in PROVIDERS.items()},
        "keys": {p: bool(env.get(k, "")) for p, (_, k) in PROVIDERS.items() if k},
        "gender": env.get("CANDIDATE_GENDER", "neutral"),
        "min_fit": env.get("MIN_FIT", "5"),
        "bot": BOT_ON,
    }


def load_presets() -> dict:
    try:
        d = json.loads(PRESETS.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def preset_save(body: dict) -> dict:
    name, flt = str(body.get("name", "")).strip(), body.get("filters")
    if not NAME_RE.match(name):
        return {"ok": False, "msg": "Название: до 40 символов — буквы, цифры, пробел"}
    if not isinstance(flt, dict) or len(json.dumps(flt)) > 20000:
        return {"ok": False, "msg": "Нет фильтров для сохранения"}
    try:
        build_search_url(flt)
    except ValueError as e:
        return {"ok": False, "msg": str(e)}
    p = load_presets()
    p[name] = {"filters": flt, "pages": max(1, min(int(body.get("pages") or 3), 20)), "limit": max(1, min(int(body.get("limit") or 20), 100))}
    PRESETS.parent.mkdir(exist_ok=True)
    PRESETS.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "msg": f"Сохранено: «{name}»"}


class Ctl:
    """What the Telegram bot may do: the same start/stop the GUI buttons use."""
    def busy(self) -> bool:
        return bool(PROC and PROC.poll() is None)

    def presets(self) -> dict:
        return load_presets()

    def start_search(self, name: str, submit: bool):
        p = load_presets().get(name)
        return start({"mode": "search", "submit": submit, **p}) if p else "Поиск не найден"

    def start_sync(self):
        return start({"mode": "sync"})

    def stop(self) -> bool:
        if self.busy():
            PROC.terminate()
            return True
        return False

    def tail(self, n: int) -> list[str]:
        return LOG[-n:]

    def events(self) -> list[dict]:
        return load_events(EVENTS)


def start_bot(stop: threading.Event) -> bool:
    env = read_env()
    token, chat = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", "")
    if not (TOKEN_RE.match(token) and re.fullmatch(r"-?\d{4,}", chat)):  # @username can't be verified -> no control
        return False
    threading.Thread(target=Bot(token, chat, Ctl()).run, args=(stop,), daemon=True).start()
    return True


def suggest_areas(text: str) -> dict:
    """Подсказки городов из публичного API hh.ru (id нужны для параметра area)."""
    url = "https://api.hh.ru/suggests/areas?" + urllib.parse.urlencode({"text": text[:60]})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "hh-autoapply-gui/1.0"}), timeout=8) as r:
            items = json.loads(r.read()).get("items", [])
        return {"items": [{"id": str(i["id"]), "text": str(i["text"])} for i in items[:8] if str(i.get("id", "")).isdigit()]}
    except Exception as e:
        return {"items": [], "error": f"Не удалось получить города ({type(e).__name__}). Используй кнопки ниже или вставь ссылку с hh.ru"}


def start(body: dict) -> str | None:
    global PROC
    if PROC and PROC.poll() is None:
        return "Уже выполняется задача"
    mode = body.get("mode") or ("sync" if body.get("sync") else "search" if body.get("search") or body.get("filters") else "urls")
    opts = (["--submit"] if body.get("submit") else []) + (["--debug"] if body.get("debug") else [])
    if mode == "sync":
        args = ["--sync-resumes"]
    elif mode == "search":
        try:
            q = build_search_url(body["filters"]) if isinstance(body.get("filters"), dict) else str(body.get("search", "")).strip()
        except ValueError as e:
            return str(e)
        if not q or (q.startswith("http") and not SEARCH_URL_RE.match(q)) or len(q) > 1500:
            return "Ссылка поиска должна быть вида https://hh.ru/search/vacancy?…"
        args = [f"--search={q}", f"--pages={max(1, min(int(body.get('pages') or 3), 20))}",
                f"--limit={max(1, min(int(body.get('limit') or 20), 100))}", *opts]
    else:
        urls = [u.strip() for u in str(body.get("urls", "")).splitlines() if u.strip()]
        bad = [u for u in urls if not URL_RE.match(u)]
        if not urls or bad:
            return f"Нужны ссылки вида https://hh.ru/vacancy/123… (не подходит: {bad[0][:60]})" if bad else "Вставь хотя бы одну ссылку"
        f = ROOT / "data" / "gui_urls.txt"
        f.parent.mkdir(exist_ok=True)
        f.write_text("\n".join(urls), encoding="utf-8")
        args = ["--file", str(f), *opts]
    env = {**os.environ, "AUTO_SUBMIT": "1" if body.get("submit") else "0", "PYTHONIOENCODING": "utf-8"}
    LOG.append("$ python app.py " + " ".join(a if not a.startswith(str(ROOT)) else Path(a).name for a in args))
    PROC = subprocess.Popen([sys.executable, "-u", "app.py", *args], cwd=ROOT, env=env, text=True,
                            encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    threading.Thread(target=_reader, args=(PROC,), daemon=True).start()


class H(BaseHTTPRequestHandler):
    def _send(self, data, ctype="application/json"):
        raw = data.encode() if isinstance(data, str) else data
        self.send_response(200); self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def _host_ok(self):  # защита от DNS-rebinding
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def do_GET(self):
        if not self._host_ok():
            return self.send_error(403)
        if self.path.startswith("/log"):
            n = int(self.path.split("n=")[-1] or 0) if "n=" in self.path else 0
            return self._send(json.dumps({"lines": LOG[n:], "n": len(LOG), "busy": bool(PROC and PROC.poll() is None)}))
        if self.path == "/settings":
            return self._send(json.dumps(get_settings()))
        if self.path == "/presets":
            return self._send(json.dumps(load_presets()))
        if self.path.startswith("/areas"):
            text = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("text", [""])[0].strip()
            return self._send(json.dumps(suggest_areas(text) if len(text) >= 2 else {"items": []}))
        self._send(PAGE, "text/html")

    def do_POST(self):  # кастомный заголовок: чужой сайт не сможет послать такой запрос без preflight
        if not self._host_ok() or self.headers.get("X-GUI") != "1":
            return self.send_error(403)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            return self.send_error(400)
        if self.path == "/stop":
            if PROC and PROC.poll() is None:
                PROC.terminate()
            return self._send("{}")
        if self.path in ("/settings", "/tg_test"):
            return self._send(json.dumps(tg_settings(body, self.path == "/tg_test")))
        if self.path == "/preset_save":
            return self._send(json.dumps(preset_save(body)))
        if self.path == "/preset_del":
            p = load_presets()
            p.pop(str(body.get("name", "")), None)
            PRESETS.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
            return self._send("{}")
        if self.path == "/ai":
            return self._send(json.dumps(ai_settings(body)))
        if self.path in ("/build", "/parse"):
            try:
                if self.path == "/build":
                    return self._send(json.dumps({"url": build_search_url(body.get("filters") or {})}))
                return self._send(json.dumps({"filters": parse_search_url(str(body.get("url", "")))}))
            except (ValueError, TypeError, AttributeError) as e:
                return self._send(json.dumps({"error": str(e)}))
        self._send(json.dumps({"error": start(body)} if self.path == "/run" else {}))

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    bot_stop = threading.Event()
    BOT_ON = start_bot(bot_stop)
    print(f"Открой http://127.0.0.1:{PORT}  (Ctrl+C — выход)" + ("  | Telegram-бот запущен" if BOT_ON else ""))
    if "--no-open" not in sys.argv:
        webbrowser.open(f"http://127.0.0.1:{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        bot_stop.set()
        if PROC and PROC.poll() is None:
            PROC.terminate()
