# 시나리오 한 장 그림(14~27) — 정본은 여기다(2026-11-14 graphio docs/jp/scenario-pictures 에서 옮겼다).
# gen-ui 시나리오 화면이 `/static/scenario-pictures/no<N>.svg` 를 요약 세 칸 위에 보여 준다.
#   python gen.py                  → 이 폴더에 no14.svg ~ no27.svg
#   python gen.py <모아보기.html>   → 위에 더해 14장 모아 보기 HTML 도 쓴다
# 이름은 시나리오 표시 이름(`control.SCENARIOS`)과 같은 쉬운 이름이다 — 바꾸면 두 곳을 함께 고친다
import html,os,sys
E_=html.escape
RED="#d93636";GRY="#8a94a3";INK="#1f2933";BLU="#2f6fd6";GRN="#2e9e5b";LBG="#f4f6f9"
# ---------- 아이콘 (도형으로) ----------
def bolt(x,y,s=1,c=RED):
    return f'<polygon transform="translate({x},{y}) scale({s})" points="6,-22 -10,4 0,4 -6,22 12,-6 2,-6 8,-22" fill="{c}"/>'
def person(x,y,s=1,c=INK):
    return f'<g transform="translate({x},{y}) scale({s})" fill="{c}"><circle cx="0" cy="-10" r="5"/><path d="M-8,10 Q-8,-3 0,-3 Q8,-3 8,10 Z"/></g>'
def crowd(x,y,c=INK):
    return "".join(person(x+dx,y+dy,0.9,c) for dx,dy in [(-22,6),(-8,0),(8,6),(22,0),(-15,18),(0,14),(15,18)])
def gear(x,y,c=RED):
    t="".join(f'<rect x="-3.5" y="-20" width="7" height="9" fill="{c}" transform="rotate({a})"/>' for a in range(0,360,45))
    return f'<g transform="translate({x},{y})">{t}<circle r="13" fill="{c}"/><circle r="5" fill="#fff"/></g>'
def cloud(x,y,c=GRY):
    return f'<g transform="translate({x},{y})" fill="{c}"><circle cx="-12" cy="4" r="10"/><circle cx="2" cy="-4" r="13"/><circle cx="15" cy="5" r="9"/><rect x="-12" y="4" width="27" height="10"/></g>'
def tower(x,y,c=INK,s=1):
    return (f'<g transform="translate({x},{y}) scale({s})" stroke="{c}" stroke-width="2.4" fill="none" stroke-linecap="round">'
            f'<path d="M-9,18 L0,-10 L9,18 M-5,6 L5,6"/><circle cx="0" cy="-12" r="2.5" fill="{c}"/>'
            f'<path d="M-8,-19 Q-13,-12 -8,-5 M8,-19 Q13,-12 8,-5"/></g>')
def phone(x,y,c=INK,s=1):
    return (f'<g transform="translate({x},{y}) scale({s})"><rect x="-11" y="-19" width="22" height="38" rx="4" fill="none" stroke="{c}" stroke-width="2.4"/>'
            f'<circle cx="0" cy="14" r="1.8" fill="{c}"/></g>')
def server(x,y,c=INK):
    return (f'<g transform="translate({x},{y})" fill="none" stroke="{c}" stroke-width="2.2">'
            + "".join(f'<rect x="-13" y="{yy}" width="26" height="9" rx="2"/><circle cx="-7" cy="{yy+4.5}" r="1.4" fill="{c}"/>' for yy in (-15,-4,7)) + '</g>')
def rings(x,y,c=RED):
    return "".join(f'<circle cx="{x}" cy="{y}" r="{r}" fill="none" stroke="{c}" stroke-width="2" opacity="{o}"/>' for r,o in [(6,1),(14,.75),(23,.5),(32,.3)])+f'<circle cx="{x}" cy="{y}" r="3" fill="{c}"/>'
def train(x,y,c=INK):
    return (f'<g transform="translate({x},{y})"><ellipse rx="30" ry="20" fill="none" stroke="{GRN}" stroke-width="4"/>'
            f'<rect x="-12" y="-28" width="24" height="14" rx="4" fill="{c}"/><rect x="-8" y="-25" width="6" height="5" fill="#fff"/><rect x="2" y="-25" width="6" height="5" fill="#fff"/></g>')
def badge(x,y,t,c=BLU):
    return f'<g transform="translate({x},{y})"><rect x="-15" y="-10" width="30" height="20" rx="5" fill="{c}"/><text y="5" text-anchor="middle" font-size="12" font-weight="700" fill="#fff">{t}</text></g>'
def plus(x,y,c=GRN):
    return f'<g transform="translate({x},{y})" fill="{c}"><rect x="-3" y="-11" width="6" height="22"/><rect x="-11" y="-3" width="22" height="6"/></g>'
def upload(x,y,c=RED):
    return f'<g transform="translate({x},{y})" fill="{c}"><polygon points="0,-16 -11,-3 -4,-3 -4,12 4,12 4,-3 11,-3"/></g>'
def dns(x,y,c=RED):
    return f'<g transform="translate({x},{y})"><rect x="-17" y="-11" width="34" height="22" rx="4" fill="{c}"/><text y="5" text-anchor="middle" font-size="12" font-weight="700" fill="#fff">DNS</text></g>'
def clock(x,y,c=RED):
    return f'<g transform="translate({x},{y})" fill="none" stroke="{c}" stroke-width="2.4"><circle r="13"/><path d="M0,-7 L0,0 L6,4"/></g>'
def x_mark(x,y,c=RED,s=1):
    return f'<g transform="translate({x},{y}) scale({s})" stroke="{c}" stroke-width="3.2" stroke-linecap="round"><path d="M-6,-6 L6,6 M6,-6 L-6,6"/></g>'
def steps(x,y,c=RED):
    return f'<polyline transform="translate({x},{y})" points="-26,10 -8,10 -8,10 -8,-8 26,-8" fill="none" stroke="{c}" stroke-width="3.2"/>'
# ---------- 망 띠 ----------
NODES=[("UE","단말",phone),("ENB","기지국",tower),("MME","MME/AMF",None),("SGW","SGW/UPF",None),
       ("PGW","PGW",None),("IMS","IMS(음성)",None),("DNS","DNS",None),("SRV","인터넷 서버",server)]
def net(x0,y0,hot,warn=()):
    out=[];w=58
    out.append(f'<line x1="{x0}" y1="{y0}" x2="{x0+w*7}" y2="{y0}" stroke="#c9d1db" stroke-width="3"/>')
    for i,(k,lab,ic) in enumerate(NODES):
        cx=x0+i*w; h=k in hot; wn=k in warn
        col=RED if h else ("#e08a00" if wn else "#5b6675")
        fill="#fdeaea" if h else ("#fff4e0" if wn else "#fff")
        out.append(f'<circle cx="{cx}" cy="{y0}" r="20" fill="{fill}" stroke="{col}" stroke-width="{3.5 if h else 2}"/>')
        if ic: out.append(ic(cx,y0,col,0.62) if ic is not server else server(cx,y0+1,col).replace("translate","translate").replace(f"translate({cx},{y0+1})",f"translate({cx},{y0+1}) scale(.62)"))
        else: out.append(f'<text x="{cx}" y="{y0+4}" text-anchor="middle" font-size="9.5" font-weight="700" fill="{col}">{E_(k)}</text>')
        out.append(f'<text x="{cx}" y="{y0+42}" text-anchor="middle" font-size="10.5" fill="{col}" font-weight="{700 if h else 400}">{E_(lab)}</text>')
        if h: out.append(f'<circle cx="{cx}" cy="{y0}" r="27" fill="none" stroke="{RED}" stroke-width="1.6" stroke-dasharray="4 3"/>')
    return "".join(out)
def arrow(x1,y,x2,c="#9aa5b4"):
    return f'<line x1="{x1}" y1="{y}" x2="{x2-8}" y2="{y}" stroke="{c}" stroke-width="3"/><polygon points="{x2},{y} {x2-11},{y-7} {x2-11},{y+7}" fill="{c}"/>'
def wrap(t,n):
    out,cur=[], ""
    for ch in t:
        cur+=ch
        if len(cur)>=n and ch==" ": out.append(cur.strip()); cur=""
    if cur.strip(): out.append(cur.strip())
    # 공백 없는 긴 줄 강제 분할
    res=[]
    for l in out:
        while len(l)>n+4: res.append(l[:n]); l=l[n:]
        res.append(l)
    return res
def textblock(x,y,t,n,size=13,col=INK,weight=400,lh=18,anchor="start"):
    return "".join(f'<text x="{x}" y="{y+i*lh}" font-size="{size}" fill="{col}" font-weight="{weight}" text-anchor="{anchor}">{E_(l)}</text>' for i,l in enumerate(wrap(t,n)))
# ---------- 카드 ----------
def card(s):
    W,H=1180,300
    o=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="-apple-system,\'Apple SD Gothic Neo\',\'Hiragino Sans\',sans-serif">',
       f'<rect width="{W}" height="{H}" rx="14" fill="#fff" stroke="#dde2e8"/>',
       f'<text x="24" y="38" font-size="20" font-weight="800" fill="{INK}">{s["no"]}  {E_(s["name"])}</text>']
    # 원인
    o.append(f'<rect x="24" y="58" width="230" height="150" rx="10" fill="#fdf1f1" stroke="#f1c2c2"/>')
    o.append(f'<text x="38" y="80" font-size="12" font-weight="700" fill="{RED}">원인</text>')
    o.append(s["icon"](139,118))
    o.append(textblock(139,168,s["cause"],15,13,INK,600,17,"middle"))
    o.append(arrow(258,133,300))
    # 망 위치
    o.append(f'<rect x="304" y="58" width="470" height="150" rx="10" fill="{LBG}" stroke="#dde2e8"/>')
    o.append(f'<text x="318" y="80" font-size="12" font-weight="700" fill="#5b6675">망 어디에서</text>')
    o.append(net(334,122,s["hot"],s.get("warn",())))
    o.append(textblock(539,192,s["where"],48,12.5,"#3b434f",400,16,"middle"))
    o.append(arrow(778,133,820))
    # 증상
    o.append(f'<rect x="824" y="58" width="332" height="150" rx="10" fill="#fff8ec" stroke="#f0d9a8"/>')
    o.append(f'<text x="838" y="80" font-size="12" font-weight="700" fill="#b36b00">이용자에게 보이는 것</text>')
    o.append(phone(866,135,INK,1.3)); o.append(s.get("sym_icon",lambda x,y:"")(866,135))
    o.append(textblock(900,112,s["symptom"],17,14,INK,700,20))
    # 결과(판정) + 헷갈리는 것
    o.append(f'<rect x="24" y="222" width="560" height="60" rx="10" fill="#fff" stroke="#bfc7d2" stroke-dasharray="6 4"/>')
    o.append(f'<text x="38" y="244" font-size="12" font-weight="700" fill="{GRY}">헷갈리는 것 (구분해야 함)</text>')
    o.append(textblock(38,266,s["confuse"],52,13,"#4a5260"))
    o.append(f'<rect x="600" y="222" width="556" height="60" rx="10" fill="#eef8f2" stroke="#b9e0c8"/>')
    o.append(f'<text x="614" y="244" font-size="12" font-weight="700" fill="{GRN}">결과 — 시스템이 내리는 결론</text>')
    o.append(textblock(614,266,s["result"],52,13.5,INK,700))
    o.append('</svg>')
    return "".join(o)
def sym_x(x,y): return x_mark(x,y-2,RED,1.1)
def sym_slow(x,y): return clock(x,y-2,RED)
S=[
 dict(no=14,name="저녁 러시로 역 주변 기지국이 붐빔",icon=lambda x,y:crowd(x,y),cause="퇴근 시간 역 주변에 사람이 몰림",hot=("ENB",),
      where="역 주변 기지국이 붐빈다. 장비는 멀쩡하다.",symptom="통화·접속은 되는데 인터넷이 느려짐",sym_icon=sym_slow,
      confuse="기지국 고장 — 고장이면 접속 자체가 실패한다",result="기지국 용량 혼잡 (사람이 몰려서 생긴 일)"),
 dict(no=15,name="야마노테선에서 4G↔5G 넘어갈 때 잠깐 멈춤",icon=lambda x,y:train(x,y+4),cause="전철로 이동하며 4G·5G를 오감",hot=("ENB",),
      where="선로를 따라 늘어선 기지국에서 4G↔5G 넘김이 잦다.",symptom="넘어가는 순간 몇 초씩 다운로드가 멈춤",sym_icon=sym_slow,
      confuse="전파가 약한 지역 — 그쪽은 멈춤이 아니라 계속 느리다",result="핸드오버(넘김) 중 끊김"),
 dict(no=16,name="5G는 잡히는데 느림 (4G 앵커 붐빔)",icon=lambda x,y:badge(x-16,y,"4G","#5b6675")+badge(x+18,y,"5G",RED)+bolt(x+1,y-1,0.5,"#e08a00"),
      cause="5G를 붙잡아 주는 4G 기지국이 붐빔",hot=("ENB",),where="5G를 쓰려면 4G 기지국(앵커)을 거치는데, 그 4G가 막힌다.",
      symptom="5G 표시는 뜨는데 5G 속도가 안 나옴",sym_icon=sym_slow,confuse="5G 기지국 고장 — 그러면 5G 표시가 사라진다",result="5G 품질 저하 (4G 앵커 경합)"),
 dict(no=17,name="5G 장애로 4G로 몰림",icon=lambda x,y:badge(x-14,y-8,"5G",RED)+x_mark(x+18,y-8)+'<path d="M%d,%d L%d,%d" stroke="#5b6675" stroke-width="2.5"/>'%(x-14,y+4,x-14,y+18)+badge(x-14,y+26,"4G","#5b6675"),
      cause="5G 단독망(SA)에 장애",hot=("MME",),warn=("ENB",),where="5G 코어가 막혀 5G 단말이 그 지역의 4G로 한꺼번에 내려온다.",
      symptom="5G가 안 잡히고 4G로만 붙음",sym_icon=lambda x,y:badge(x,y-2,"4G","#5b6675"),
      confuse="4G에 갑자기 사람이 몰린 것 — 늘어난 쪽이 전부 5G 단말인지 본다",result="5G 쪽 장애 추정 (폴백 급증)"),
 dict(no=18,name="데이터 관문 자원 바닥 (5G UPF)",icon=lambda x,y:server(x,y)+bolt(x+22,y-14,0.6),cause="데이터 관문 장비(SGW/UPF)의 자원이 바닥남",hot=("SGW",),
      where="새 데이터 연결을 만들어 줄 자리가 없어 요청을 거절한다.",symptom="새로 데이터 접속하면 실패",sym_icon=sym_x,
      confuse="단말·베어러 설정 오류 — 뒤따라 나는 거절일 뿐, 처음 막힌 곳이 원인",result="게이트웨이 자원 소진"),
 dict(no=19,name="접속 관리 장비 과부하가 번짐 (5G AMF)",icon=lambda x,y:server(x,y)+'<g fill="none" stroke="%s" stroke-width="2"><circle cx="%d" cy="%d" r="24" opacity=".6"/><circle cx="%d" cy="%d" r="32" opacity=".3"/></g>'%(RED,x,y,x,y),
      cause="접속 관리 장비(MME/AMF) 과부하",hot=("MME",),where="접속을 처리하는 장비가 밀려, 걸리는 지역이 점점 넓어진다.",
      symptom="접속이 늦거나 실패, 통화가 끊김",sym_icon=sym_x,confuse="여러 기지국이 동시에 고장 — 공통으로 지나는 장비 하나를 본다",result="MME 과부하"),
 dict(no=20,name="음성 통화만 안 걸림 (VoNR·IMS)",icon=lambda x,y:'<text x="%d" y="%d" font-size="30" text-anchor="middle">☎</text>'%(x,y+10)+bolt(x+24,y-12,0.6),
      cause="음성 서비스 장비(IMS) 접속 장애",hot=("IMS",),where="음성 통화용 연결만 막히고 일반 데이터 연결은 정상이다.",
      symptom="전화만 안 걸리고 인터넷은 됨",sym_icon=sym_x,confuse="단말 전체 접속 장애 — 데이터까지 막히는지 본다",result="IMS 접속 장애"),
 dict(no=21,name="동영상 서버(CDN) 느림",icon=lambda x,y:cloud(x,y)+bolt(x+26,y-12,0.6),cause="동영상 서버(CDN) 하나가 느리게 응답",hot=("SRV",),
      where="망은 정상. 망 밖의 서버 한 곳이 느리다.",symptom="그 동영상 앱만 멈추고 다시 불러옴",sym_icon=sym_slow,
      confuse="지역 망 혼잡 — 같은 곳의 다른 앱은 멀쩡하다",result="외부 서버 이슈"),
 dict(no=22,name="결제 앱이 느림 — 주소 찾기(DNS) 지연",icon=lambda x,y:dns(x,y),cause="주소 찾기 장비(DNS)가 느리고 일부 실패",hot=("DNS",),
      where="결제 앱 주소를 찾는 단계에서 늦어지고 실패한다.",symptom="결제 앱이 늦게 뜨거나 끊김",sym_icon=sym_slow,
      confuse="결제 서버 고장 — 서버까지 가기 전 주소 찾기에서 막혔다",result="DNS 저하"),
 dict(no=23,name="행사장 군중으로 사진 올리기 막힘",icon=lambda x,y:crowd(x-8,y)+upload(x+36,y-4),cause="행사장에 사람이 몰려 사진·영상을 올림",hot=("ENB",),
      where="행사장 기지국의 올리는 쪽(업링크)이 가득 찬다.",symptom="사진·영상 올리기가 안 됨",sym_icon=lambda x,y:upload(x,y-2),
      confuse="기지국 고장 — 내려받기는 비교적 버틴다",result="행사 군중 혼잡"),
 dict(no=24,name="기지국 증설 뒤 손님이 옮겨 감 (장애 아님)",icon=lambda x,y:tower(x-14,y,INK)+plus(x+20,y-10),cause="새 기지국을 세움 (장애 아님)",hot=(),warn=("ENB",),
      where="붐비던 기지국의 손님이 새 기지국으로 옮겨 간다.",symptom="붐비던 곳이 점점 풀림",sym_icon=lambda x,y:'<text x="%d" y="%d" font-size="22" text-anchor="middle" fill="%s">✓</text>'%(x,y+6,GRN),
      confuse="새 기지국 고장·트래픽 급감 — 줄어든 만큼 옆에서 늘었는지 본다",result="정상 변화 — 장애로 판정하지 않음"),
 dict(no=25,name="설정 변경 직후 통화 끊김이 계단처럼 늘어남 (SON)",icon=lambda x,y:gear(x-14,y)+steps(x+22,y+2),cause="자동 최적화(SON)가 기지국 1~2곳 설정을 바꿈",hot=("ENB",),
      where="그 기지국의 옆 셀로 넘기는 설정이 바뀐다.",symptom="이동 중 통화가 끊기는 일이 한 번에 늘어 그대로",sym_icon=sym_x,
      confuse="장비 고장 — 고장은 들쭉날쭉, 설정 변경은 한 시각에 계단처럼",result="설정 변경 의심 (바뀐 시각 제시)"),
 dict(no=26,name="SW 업데이트한 특정 기종만 접속 실패",icon=lambda x,y:phone(x-10,y,RED,1.2)+'<path d="M%d,%d a10,10 0 1,1 -4,-8" fill="none" stroke="%s" stroke-width="2.5"/>'%(x+30,y+6,RED),
      cause="특정 기종의 소프트웨어 업데이트",hot=("UE",),where="망은 정상. 그 기종만 등록 단계에서 실패한다.",
      symptom="그 기종만 망에 붙지 못함",sym_icon=sym_x,confuse="지역·MME 장애 — 다른 기종은 같은 곳에서 멀쩡하다",result="단말 기종 문제"),
 dict(no=27,name="지진 — 통화 폭주·기지국 정지",icon=lambda x,y:rings(x,y),cause="도쿄 밖(바다·땅속 깊은 곳) 지진",hot=("ENB",),
      where="넓은 지역에 안부 통화가 몰린다. 무른 땅 기지국 몇 곳만 꺼진다(0곳일 수도).",symptom="통화가 몰려 잘 안 걸리고, 일부 지역은 불통",sym_icon=sym_x,
      confuse="행사 군중 폭주 — 지진 정보(진앙·규모)와 시각이 맞는지 본다",result="지진 (지진 정보 + 피해 조합)"),
]
cards="".join(f'<div class=c>{card(s)}</div>' for s in S)
legend=("<div class=lg><b>읽는 법</b> 왼쪽부터 <span style='color:%s'>원인</span> → <span style='color:#5b6675'>망 어디에서</span>(빨간 원이 고장 위치, 주황은 영향만) → "
        "<span style='color:#b36b00'>이용자에게 보이는 것</span>. 아래 왼쪽은 헷갈리기 쉬운 다른 장애, 오른쪽은 시스템의 결론입니다.</div>")%RED
HERE=os.path.dirname(os.path.abspath(__file__))
if len(sys.argv)>1:
  open(sys.argv[1],"w").write("<!doctype html><meta charset=utf-8><title>시나리오 한 장 그림 (14~27)</title>"
   "<style>body{margin:24px;background:#eef1f5;font-family:-apple-system,'Apple SD Gothic Neo',sans-serif}.c{margin:0 0 18px}.lg{margin:0 0 18px;font-size:14px;color:#333}</style>"
   f"<h2>장애 시나리오 한 장 그림 — 원인 → 망 위치 → 증상 → 결론</h2>{legend}{cards}")
for s in S: open(os.path.join(HERE,f"no{s['no']}.svg"),"w",encoding="utf-8").write(card(s))
