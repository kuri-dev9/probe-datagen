
(function(){
  const g=id=>document.getElementById(id);
  // 주입 화면은 같은 조회를 먼저 내 두었다 — 그 응답을 함께 받는다(`getJson`). 다른 화면엔 없다
  const gj=window.getJson||(u=>fetch(u).then(r=>r.json()));
  langSelect('gp-lang');
  const pad=n=>String(n).padStart(2,'0');
  //: 가상 시각 오프셋(초) — `/api/status` 가 곡선 켬일 때만 준다. 시계 옆에 "가상 HH:MM"
  let vo=0;
  const clock=()=>{ const d=new Date(); const v=new Date(d.getTime()+vo*1000);
    g('gp-clock').textContent=
    `${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
    + (vo ? ` · ${T('가상')} ${pad(v.getHours())}:${pad(v.getMinutes())}` : ''); };
  clock(); setInterval(clock, 1000);
  // **장애 주입 화면의 1초 폴링과 별개다** — 그 화면만의 상태줄이고, 이쪽은
  // 세 화면 공통이라 가볍게 5초다
  const tick=async()=>{
    try{
      const st=await gj('/api/status');
      vo=Number(st.virtual_offset_sec||0);
      const on=st.gen_enabled!==false, act=(st.active||[]).length;
      g('gp-gen-t').textContent=on ? T('생성 중') : T('정지');
      g('gp-gen-t').style.color=on ? '#4caf7d' : '#8a94a3';
      g('gp-dot').style.background=!on ? '#6b7684' : act ? '#e5534b'
        : (st.blocked||[]).length ? '#d8a22a' : '#4caf7d';
      let slot='—', age=null;
      if(st.last_slot){ slot=String(st.last_slot).replace('T',' ').slice(11,16);
        if(st.now) age=Math.round((new Date(String(st.now).replace(' ','T'))
          - new Date(String(st.last_slot).replace(' ','T')))/60000); }
      g('gp-slot').textContent=slot;
      // 멈춘 것을 정상으로 읽는 쪽이 가장 위험하다 — 3분 넘으면 노랗게
      g('gp-slot').style.color=age!=null&&age>3 ? '#d8a22a' : '';
      g('gp-slot').title=age==null ? '' : T('({n}분 전)',{n:age});
      g('gp-pct').textContent=st.volume_pct==null ? '—' : Math.round(st.volume_pct)+'%';
      g('gp-act').textContent=act;
      g('gp-act').style.color=act ? '#e5534b' : '';
    }catch(e){ g('gp-gen-t').textContent=T('서버 응답 없음'); g('gp-dot').style.background='#e5534b'; }
  };
  tick(); setInterval(tick, 5000);
  // 오늘 생성 — 인입 현황(전 카드)의 값을 그대로 쓴다(`/api/ingest`). `CM_DATAGEN_HISTORY` 를
  // 훑는 조회라 1분 주기다. 오늘 행이 없으면 0 이 사실이다(자정 직후·하루 내내 정지)
  const esc2=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const fmt=n=>Number(n||0).toLocaleString();
  const today=async()=>{
    try{
      const d=await gj('/api/ingest?minutes=60');
      const lx=new Date(), ymd=`${lx.getFullYear()}-${pad(lx.getMonth()+1)}-${pad(lx.getDate())}`;
      const t0=(d.by_day||[])[0], isT=t0&&String(t0.d).slice(0,10)===ymd;
      const ts=d.today_series||[], tsum=ts.reduce((a,x)=>a+(x.recs||0),0);
      g('gp-today').textContent=T('{n}건',{n:fmt(tsum)});
      const tot=d.total||{};
      const cum=(d.by_day||[]).reduce((a,x)=>a+(x.slots||0),0);
      g('gp-today-pop').innerHTML=
        `<h4>${esc2(T('오늘'))}</h4>`
        +`<div class="r"><span>${esc2(T('생성한 분'))}</span><span>${esc2(T('{n}분',{n:isT?t0.slots:0}))}</span></div>`
        +ts.map(r=>`<div class="r"><span>${esc2(r.series)}</span><span>${esc2(T('{n}건',{n:fmt(r.recs)}))}</span></div>`).join('')
        +`<div class="r t"><span>${esc2(T('합계'))}</span><span>${esc2(T('{n}건',{n:fmt(tsum)}))}</span></div>`
        +`<h4 style="margin-top:9px">${esc2(T('최근 {n}분 · 계열별',{n:d.minutes||60}))}</h4>`
        +(d.series||[]).map(r=>`<div class="r"><span>${esc2(r.series)}</span>`
          +`<span>${esc2(T('{n}건',{n:fmt(r.recs)}))} · ${esc2(r.per_min)}/${esc2(T('분'))}</span></div>`).join('')
        +`<div class="r t"><span>${esc2(T('합계'))}</span><span>${esc2(T('{n}건',{n:fmt(tot.records)}))} · ${esc2(tot.per_min||0)}/${esc2(T('분'))}</span></div>`
        +`<div style="margin-top:6px"><small>${esc2(T('누적 {b}분',{b:cum}))}`
        +`${(d.by_day||[]).length>1?esc2(T('({n}일)',{n:d.by_day.length})):''}</small></div>`;
    }catch(e){ g('gp-today').textContent='—'; }
  };
  today(); setInterval(today, 60000);
  // 생성 지역 — 저장값이 정본이다(graphio 상단 바도 같은 키를 본다)
  gj('/api/region').then(r=>{
    const v=r.saved||r.file||''; const L={};
    (r.locales||[]).forEach(l=>{ L[l.value]=T(l.label); });
    g('gp-loc').textContent=String(v).toUpperCase()||'—';
    g('gp-loc').title=T('생성 지역 — gen-ui 의 지역 설정')+(L[v]?' · '+L[v]:'')
      +(r.pending&&r.pending!==v?' → '+(L[r.pending]||r.pending):'');
  }).catch(()=>{});
})();
