const $ = (id) => document.getElementById(id);
const all = (selector) => [...document.querySelectorAll(selector)];
const readForm = (form) => Object.fromEntries(new FormData(form));
const labels = {manager:"主管",operator:"投手",client:"客户只读",queued:"排队中",running:"处理中",
  review:"待审核",published:"已发布",failed:"失败"};
const money = (value) => value == null ? "—" : value.toLocaleString("zh-CN",{maximumFractionDigits:2});
const percent = (value) => value == null ? "—" : (value*100).toFixed(2)+"%";
const show = (id,message,error=false) => {
  $(id).textContent = message;
  $(id).classList.toggle("error",error);
};

let user, tab = "overview", jobId, timer, currentMarkdown = "";
let view = 0;
function el(tag, content, className) {
  const node = document.createElement(tag);
  if (content !== undefined) node.textContent = content;
  if (className) node.className = className;
  return node;
}

async function api(path, body) {
  const response = await fetch("/api/"+path, body === undefined ? {} : {
    method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)
  });
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== "login") loggedOut();
    throw Error(result.error || "请求失败");
  }
  return result;
}

function loggedOut() {
  view++;
  clearTimeout(timer);
  user = null;
  jobId = undefined;
  currentMarkdown = "";
  $("workspace").hidden = true;
  $("login").hidden = false;
  $("login-form").reset();
  for (const id of ["interpretation","import-result","evidence","jobs","diagnosis-detail","query-results","usage","events"]) {
    $(id).replaceChildren();
  }
  $("query-chart").hidden = true;
}

function withStatus(action, button, login = false) {
  return async (event) => {
    event?.preventDefault();
    const target = login ? "login-status" : "status";
    const submitter = button || event?.submitter;
    if (submitter) submitter.disabled = true;
    show(target,"");
    try { await action(event); }
    catch (error) { show(user && !login ? target : "login-status",error.message,true); }
    finally { if (submitter) submitter.disabled = false; }
  };
}

function scope() {
  const account = $("account").value;
  if (!account) throw Error("当前用户没有授权账户");
  return {account,start:$("start").value,end:$("end").value};
}

function datesForAccount() {
  const account = user.accounts.find((a) => a.id === $("account").value);
  const today = new Date();
  const date = new Date(today.getTime()-today.getTimezoneOffset()*60000).toISOString().slice(0,10);
  $("start").value = account?.latest || date;
  $("end").value = account?.latest || date;
}

async function session() {
  user = await api("me");
  $("account").replaceChildren(...user.accounts.map((account) => new Option(account.name,account.id)));
  $("username").textContent = user.username;
  $("role").textContent = labels[user.role];
  all("[data-editor]").forEach((e) => { e.hidden = user.role === "client"; });
  all("[data-manager]").forEach((e) => { e.hidden = user.role !== "manager"; });
  $("login").hidden = true;
  $("workspace").hidden = false;
  datesForAccount();
  await switchTab("overview");
}

function table(container, columns, rows) {
  if (!rows.length) {
    container.replaceChildren(el("p","暂无数据","subtle"));
    return;
  }
  const t = el("table");
  const heading = t.createTHead().insertRow();
  for (const label of columns) {
    const th = el("th",label);
    th.scope = "col";
    heading.append(th);
  }
  const body = t.createTBody();
  for (const row of rows) {
    const tr = body.insertRow();
    for (const value of row) tr.append(el("td",value ?? "—"));
  }
  container.replaceChildren(t);
}

function chart(canvas, items, unit = money) {
  const ctx = canvas.getContext("2d");
  const width = canvas.width, height = canvas.height;
  ctx.clearRect(0,0,width,height);
  ctx.font = "17px system-ui";
  ctx.fillStyle = "#4f6961";
  if (!items.length) {
    ctx.fillText("暂无数据",24,30);
    return;
  }
  const number = items.map((p) => p.value ?? 0);
  const max = Math.max(1e-6,...number);
  const x = 105, top = 18, bottom = height-36, w = width-x-24, h = bottom-top;
  for (let i=0; i<=4; i++) {
    const y = bottom-h*i/4;
    ctx.strokeStyle = "#dbe6e1";
    ctx.beginPath(); ctx.moveTo(x,y); ctx.lineTo(x+w,y); ctx.stroke();
    ctx.fillText(unit(max*i/4),4,y+5);
  }
  const gap = w/number.length;
  for (const [i,value] of number.entries()) {
    if (items[i].value == null) continue;
    const amount = value*h/max;
    ctx.fillStyle = "#13836a";
    ctx.fillRect(x+gap*i+gap*.16,bottom-amount,gap*.68,amount);
  }
  ctx.fillStyle = "#4f6961";
  ctx.textAlign = "center";
  const label = (item) => item.label.length > 11 ? item.label.slice(0,10)+"…" : item.label;
  ctx.fillText(label(items[0]),x+Math.max(gap/2,40),height-8);
  if (items.length > 1) ctx.fillText(label(items.at(-1)),x+w-Math.max(gap/2,40),height-8);
  ctx.textAlign = "left";
}

async function report(viewId) {
  if (!user.accounts.length) {
    show("status","当前用户没有授权账户");
    return;
  }
  const data = await api("reports",scope());
  if (view !== viewId) return;
  $("cards").replaceChildren(...[
    ["spend","总消耗",money],["ctr","点击率 CTR",percent],["cpc","点击成本 CPC",money],["cpl","线索成本 CPL",money],
  ].map(([key,label,format]) => {
    const item = el("article",undefined,"card kpi");
    const change = data.changes[key];
    item.append(el("span",label),el("strong",format(data.current[key])),
      el("span",change == null ? "暂无上期对比" : "较上期 "+(change>=0?"+":"")+percent(change)));
    return item;
  }));
  chart($("trend"),data.trend.map((day) => ({label:day.label,value:day.spend})));
  table($("plans"),["计划","消耗","CTR","CPL"],data.plans.map((p) => [p.label,money(p.spend),percent(p.ctr),money(p.cpl)]));
  currentMarkdown = data.markdown;
  show("markdown",currentMarkdown);
}

async function history(viewId) {
  const data = await api("history",scope());
  if (view === viewId) table($("history"),["版本","来源","日期 (UTC)","导入人","生效天数"],
    data.imports.map((row) => [row.id,row.source,row.created,row.actor,row.active_days]));
}

async function listJobs(viewId) {
  const data = await api("jobs/list",scope());
  if (view !== viewId) return;
  const buttons = data.jobs.map((job) => {
    const b = el("button",job.day+" · "+labels[job.state]+" · #"+job.id,"job-row");
    b.addEventListener("click",withStatus(() => detail(job.id,viewId),b));
    return b;
  });
  $("jobs").replaceChildren(...(buttons.length ? buttons : [el("p","暂无诊断")]));
}

function download(name,text) {
  const url = URL.createObjectURL(new Blob([text],{type:"text/markdown;charset=utf-8"}));
  const a = el("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url),1000);
}

async function detail(id,viewId) {
  clearTimeout(timer);
  jobId = id;
  const result = await api("jobs/"+id);
  if (view !== viewId || jobId !== id) return;
  const panel = $("diagnosis-detail");
  panel.hidden = false;
  panel.replaceChildren(el("h2",result.day+" · "+labels[result.state]));
  if (["queued","running"].includes(result.state)) {
    panel.append(el("p","诊断正在执行，进度会自动更新。"));
    timer = setTimeout(withStatus(() => detail(id,viewId)),800);
    return;
  }
  if (result.state === "failed") {
    panel.append(el("p",result.error));
    return;
  }
  const {work} = result;
  const indicators = "CPL "+money(work.current.cpl)+" 元 · CTR "+percent(work.current.ctr)+
    " · 参考前 7 天中的 "+work.baseline.days+" 个投放日";
  panel.append(el("h3","优先排查 "+work.stage),el("p",indicators,"subtle"));
  const checklist = el("form");
  const isReview = result.state === "review";
  work.suggestions.forEach((item,index) => {
    const title = item.text + (item.references.length ? " ["+item.references.join(", ")+"]" : "");
    if (isReview) {
      const label = el("label",undefined,"checkbox");
      const checkbox = el("input");
      checkbox.type = "checkbox";
      checkbox.name = "accept";
      checkbox.value = index;
      checkbox.checked = true;
      label.append(checkbox,document.createTextNode(title));
      checklist.append(label);
    } else {
      checklist.append(el("p",title));
    }
  });
  panel.append(checklist,...evidenceCards(work.evidence));
  if (isReview) {
    const note = el("textarea");
    note.name = "feedback";
    note.maxLength = 500;
    note.placeholder = "审核备注（可选）";
    const approve = el("button","发布选中的建议","primary");
    checklist.append(note,approve);
    checklist.addEventListener("submit",withStatus(async () => {
      const form = new FormData(checklist);
      await api("jobs/"+id+"/review",{accept:form.getAll("accept").map(Number),feedback:form.get("feedback")});
      await Promise.all([listJobs(viewId),detail(id,viewId)]);
    },approve));
  } else {
    const button = el("button","下载 Markdown");
    button.addEventListener("click",() => download("诊断-"+result.day+".md",[
      "# 诊断报告 "+result.day,"","排查环节 "+work.stage,indicators,"","## 建议",
      ...work.suggestions.map((item) => "- "+item.text+" ["+item.references.join(", ")+"]"),
      "","## 参考",...work.evidence.map((e) => "- ["+e.id+"] "+e.title+" · "+e.source)
    ].join("\n")));
    panel.append(el("div",undefined,"actions"));
    panel.lastChild.append(button);
  }
}

function evidenceCards(evidence) {
  return evidence.map((item) => {
    const card = el("article",undefined,"excerpt");
    card.append(el("h3",item.title),el("p","["+item.id+"] "+item.source,"subtle"),el("p",item.excerpt));
    return card;
  });
}

async function switchTab(next) {
  show("status","");
  tab = next;
  const id = ++view;
  clearTimeout(timer);
  jobId = undefined;
  $("diagnosis-detail").hidden = true;
  $("query-chart").hidden = true;
  $("query-results").replaceChildren();
  $("evidence").replaceChildren();
  $("interpretation").replaceChildren();
  all(".panel").forEach((panel) => { panel.hidden = panel.id !== tab; });
  all("[data-tab]").forEach((button) => { button.classList.toggle("selected",button.dataset.tab === tab); });
  show("page-title",document.querySelector('[data-tab="'+tab+'"]').textContent);
  if (tab === "overview") await report(id);
  if (tab === "upload") await history(id);
  if (tab === "diagnosis") await listJobs(id);
  if (tab === "management") {
    const data = await api("management");
    if (view !== id) return;
    table($("usage"),["账户","功能","模型","输入","输出","人民币"],data.usage.map((r) =>
      [r.account_id,r.feature,r.model,r.input_tokens,r.output_tokens,r.cost]));
    table($("events"),["时间 (UTC)","用户","账户","动作","内容"],data.events.map((r) =>
      [r.created,r.name,r.account_id,r.action,r.detail]));
  }
}

all("[data-tab]").forEach((button) => button.addEventListener("click",withStatus(() => switchTab(button.dataset.tab),button)));
$("scope").addEventListener("submit",withStatus(() => switchTab(tab)));
$("account").addEventListener("change",withStatus(async () => { datesForAccount(); await switchTab(tab); }));
$("login-form").addEventListener("submit",withStatus(async () => { await api("login",readForm($("login-form"))); await session(); },null,true));
$("logout").addEventListener("click",withStatus(async () => { await api("logout",{}); loggedOut(); },$("logout")));

$("upload-form").addEventListener("submit",withStatus(async () => {
  const form = readForm($("upload-form"));
  if (!form.file.size || form.file.size > 4*1024*1024) throw Error("请选择 4 MiB 以内的 CSV");
  const bytes = new Uint8Array(await form.file.arrayBuffer());
  const parts = [];
  for (let i=0; i<bytes.length; i+=8192) parts.push(String.fromCharCode(...bytes.subarray(i,i+8192)));
  let mapping;
  if (form.mapping.trim()) {
    try { mapping = JSON.parse(form.mapping); }
    catch { throw Error("列名映射须为有效 JSON"); }
  }
  const result = await api("import",{...scope(),source:form.source,day:form.day,mapping,
    replace:form.replace === "on",file:btoa(parts.join(""))});
  const account = user.accounts.find((a) => a.id === $("account").value);
  account.latest = [account.latest,result.days.at(-1)].filter(Boolean).sort().at(-1);
  datesForAccount();
  show("import-result",(result.unchanged ? "此版本已导入" : "已导入 "+result.rows+" 行")+"，覆盖 "+result.days.length+" 天"+
    (result.missing.length ? "。缺少可选指标 "+result.missing.join("、") : ""));
  await history(view);
}));

$("ask-form").addEventListener("submit",withStatus(async () => {
  const id = view;
  const result = await api("ask",{...scope(),...readForm($("ask-form"))});
  if (id !== view) return;
  const format = ["ctr","conversion"].includes(result.metric) ? percent : money;
  table($("query-results"),["日期或计划",result.name],result.data.map((r) => [r.label,format(r.value)]));
  $("query-chart").hidden = result.data.length < 2;
  chart($("query-chart"),result.data,format);
}));

$("search-form").addEventListener("submit",withStatus(async () => {
  const id = view;
  const r = await api("knowledge",{...scope(),...readForm($("search-form"))});
  if (id === view) $("evidence").replaceChildren(...(r.evidence.length ? evidenceCards(r.evidence) : [el("p",r.message)]));
}));
$("document-form").addEventListener("submit",withStatus(async () => {
  await api("documents",{...scope(),...readForm($("document-form"))});
  $("document-form").reset();
  show("status","已保存，可在知识库中检索");
}));
$("new-job").addEventListener("click",withStatus(async () => {
  const id = view;
  const s = scope();
  const job = await api("jobs",{account:s.account,day:s.end});
  if (id === view) await Promise.all([listJobs(id),detail(job.id,id)]);
},$("new-job")));
$("copy").addEventListener("click",withStatus(async () => {
  if (!currentMarkdown) throw Error("请先查看报表");
  await navigator.clipboard.writeText(currentMarkdown);
  show("status","已复制 Markdown");
},$("copy")));
$("push").addEventListener("click",withStatus(async () => {
  if (!window.confirm("将此账户选定日期的日报发送到已配置的群机器人？")) return;
  await api("push",scope());
  show("status","已推送日报");
},$("push")));
$("insight").addEventListener("click",withStatus(async () => {
  const id = view;
  const result = await api("insight",scope());
  if (id === view) show("interpretation",result.text);
},$("insight")));

if (window.location.protocol === "file:") {
  show("login-status","当前打开的是本地 HTML。请运行 python -m app serve，再访问 http://127.0.0.1:8080。",true);
} else {
  $("login-form").querySelector("button").disabled = false;
  show("login-status","");
  session().catch((error) => {
    if (error.message !== "请登录") show("login-status",error.message,true);
  });
}
