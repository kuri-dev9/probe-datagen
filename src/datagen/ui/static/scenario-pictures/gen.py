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
        out.append(f'<text x="{cx}" y="{y0+42}" text-anchor="middle" font-size="11.5" fill="{col}" font-weight="{700 if h else 400}">{E_(lab)}</text>')
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
# ---------- 장면 그림 조각 (2026-11-16 사용자 "그림이 단순해 큰 뜻을 못 품는다 — 왼쪽은 더 세밀하게, 오른쪽도 뜻을 더") ----------
# 왼쪽 「원인」은 ①②③ 단계 장면이다. 그림 속 사실은 각 번호의 시나리오 설명(`config/scenario_how.json`)과 생성기
# 동작(scenarios.py · faults.py)에 있는 것만 쓴다 — 수치를 짓지 않는다. 24번은 설명이 11-15 개편 전 값이라 코드 값을 쓴다
AMB="#e08a00"
def chart(x,y,kind,c=RED,w=120,h=56):
    """작은 시계열 — 축 + 선. kind: step(계단) · rise(급증) · drop(꺾여 내려감) · saw(주기적 짧은 멈춤) · flat(그대로)
    · shift(A 내려가고 B 올라감) · bump(올랐다 내려옴). 모양만 보이는 개념도다(값 아님)"""
    x0,y0=x-w/2,y+h/2
    ax=f'<path d="M{x0},{y0-h} L{x0},{y0} L{x0+w},{y0}" fill="none" stroke="#9aa5b4" stroke-width="1.6"/>'
    P={"step":[(0,.8),(.45,.8),(.45,.2),(1,.2)],"rise":[(0,.85),(.35,.85),(.5,.15),(1,.2)],
       "drop":[(0,.2),(.4,.2),(.55,.8),(1,.8)],"flat":[(0,.5),(1,.5)],
       "saw":[(0,.3),(.18,.3),(.2,.85),(.24,.3),(.48,.3),(.5,.85),(.54,.3),(.78,.3),(.8,.85),(.84,.3),(1,.3)],
       "bump":[(0,.85),(.2,.85),(.35,.2),(.7,.2),(.85,.85),(1,.85)]}
    if kind=="shift":
        a=[(0,.6),(.3,.25),(.55,.25),(.65,.7),(1,.7)]; b=[(.55,.95),(.65,.45),(1,.45)]
        pa=" L".join(f"{x0+u*w:.1f},{y0-h*(1-v):.1f}" for u,v in a); pb=" L".join(f"{x0+u*w:.1f},{y0-h*(1-v):.1f}" for u,v in b)
        return ax+f'<path d="M{pa}" fill="none" stroke="{RED}" stroke-width="2.6"/><path d="M{pb}" fill="none" stroke="{GRN}" stroke-width="2.6"/>'\
            +f'<text x="{x0+4}" y="{y0-h+12}" font-size="12" fill="{RED}" font-weight="700">A</text><text x="{x0+w-12}" y="{y0-h*.55+16}" font-size="12" fill="{GRN}" font-weight="700">B</text>'
    pts=" L".join(f"{x0+u*w:.1f},{y0-h*(1-v):.1f}" for u,v in P[kind])
    return ax+f'<path d="M{pts}" fill="none" stroke="{c}" stroke-width="2.8" stroke-linejoin="round"/>'
def label(x,y,t,c=INK,s=12,w=700,a="middle"):
    return f'<text x="{x}" y="{y}" font-size="{s}" fill="{c}" font-weight="{w}" text-anchor="{a}">{E_(t)}</text>'
def arr(x1,y1,x2,y2,c="#5b6675",wd=2.4,dash=""):
    import math
    a=math.atan2(y2-y1,x2-x1); hx,hy=x2-9*math.cos(a),y2-9*math.sin(a)
    p1=(hx+5*math.sin(a),hy-5*math.cos(a)); p2=(hx-5*math.sin(a),hy+5*math.cos(a))
    d=f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<line x1="{x1}" y1="{y1}" x2="{hx:.1f}" y2="{hy:.1f}" stroke="{c}" stroke-width="{wd}"{d}/>'
            f'<polygon points="{x2},{y2} {p1[0]:.1f},{p1[1]:.1f} {p2[0]:.1f},{p2[1]:.1f}" fill="{c}"/>')
def gauge(x,y,frac,c=RED):
    """자원 막대 — 가득 찬 정도"""
    return (f'<rect x="{x-8}" y="{y-26}" width="16" height="52" rx="3" fill="#fff" stroke="{c}" stroke-width="2"/>'
            f'<rect x="{x-6}" y="{y+24-48*frac:.1f}" width="12" height="{48*frac:.1f}" rx="2" fill="{c}"/>')
def towers(x,y,states,gap=40,s=.9):
    """기지국 줄 — ok(검정) · hot(빨강) · busy(주황) · off(회색 + X) · new(초록)"""
    o=[]; n=len(states); x0=x-(n-1)*gap/2
    for i,st in enumerate(states):
        cx=x0+i*gap; col={"ok":INK,"hot":RED,"busy":AMB,"off":"#b4bcc7","new":GRN}[st]
        o.append(tower(cx,y,col,s))
        if st=="off": o.append(x_mark(cx,y-14,"#8a94a3",.7))
    return "".join(o)
def ppl(x,y,n,c=INK,gap=15,red=()):
    return "".join(person(x+(i-(n-1)/2)*gap,y,.8,RED if i in red else c) for i in range(n))
def app(x,y,c,t=""):
    return f'<rect x="{x-11}" y="{y-11}" width="22" height="22" rx="5" fill="{c}"/>'+(f'<text x="{x}" y="{y+4.5}" font-size="11" fill="#fff" text-anchor="middle" font-weight="700">{E_(t)}</text>' if t else "")
def check(x,y,c=GRN):
    return f'<path d="M{x-7},{y} L{x-2},{y+6} L{x+8},{y-6}" fill="none" stroke="{c}" stroke-width="3" stroke-linecap="round"/>'
def screen(x,y,lines,c=RED):
    """단말 화면 — 화면에 뜨는 상태 글자"""
    o=[f'<rect x="{x-46}" y="{y-62}" width="92" height="124" rx="12" fill="#fff" stroke="{INK}" stroke-width="2.6"/>',
       f'<rect x="{x-38}" y="{y-50}" width="76" height="96" rx="4" fill="{c}" opacity=".12"/>',
       f'<circle cx="{x}" cy="{y+54}" r="2.6" fill="{INK}"/>']
    for i,l in enumerate(lines):
        o.append(label(x,y-14+i*20-(len(lines)-1)*6,l,c,13.5,800))
    return "".join(o)
# ---------- 카드 ----------
W,H=1180,522  # 아래 「헷갈리는 것·결과」 줄은 11-16 사용자 요청으로 뺐다(그림은 원인·망·이용자까지)
def card(s):
    o=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="-apple-system,\'Apple SD Gothic Neo\',\'Hiragino Sans\',sans-serif">',
       f'<rect width="{W}" height="{H}" rx="14" fill="#fff" stroke="#dde2e8"/>',
       f'<text x="24" y="38" font-size="21" font-weight="800" fill="{INK}">{s["no"]}  {E_(s["name"])}</text>']
    # ① 원인 — 단계 장면
    o.append(f'<rect x="24" y="54" width="1132" height="244" rx="10" fill="#fdf1f1" stroke="#f1c2c2"/>')
    o.append(f'<text x="40" y="78" font-size="13" font-weight="700" fill="{RED}">원인 — 무슨 일이 어떻게 벌어지나</text>')
    st=s["steps"]; n=len(st); gap=34; pw=(1132-32-(n-1)*gap)/n
    for i,(draw,cap) in enumerate(st):
        px=40+i*(pw+gap); cx=px+pw/2
        o.append(f'<rect x="{px:.1f}" y="90" width="{pw:.1f}" height="196" rx="8" fill="#fff" stroke="#f3d3d3"/>')
        o.append(f'<circle cx="{px+16:.1f}" cy="106" r="11" fill="{RED}"/>'+label(px+16,110.5,"①②③④"[i],"#fff",13,800))
        o.append(draw(cx,158))
        o.append(textblock(cx,226,cap,int(pw/13.6),13.5,INK,600,18,"middle"))
        if i<n-1: o.append(arr(px+pw+4,188,px+pw+gap-4,188,RED,2.6))
    # ② 망 어디에서
    o.append(arr(330,300,330,322,"#9aa5b4",2.6))
    o.append(f'<rect x="24" y="326" width="620" height="176" rx="10" fill="{LBG}" stroke="#dde2e8"/>')
    o.append(f'<text x="40" y="350" font-size="13" font-weight="700" fill="#5b6675">망 어디에서</text>')
    o.append(net(131,398,s["hot"],s.get("warn",())))
    o.append(textblock(334,468,s["where"],50,13.5,"#3b434f",400,18,"middle"))
    o.append(arr(648,414,676,414,"#9aa5b4",3))
    # ③ 이용자에게 보이는 것 — 화면 · 누가 · 겪는 순서
    o.append(f'<rect x="680" y="326" width="476" height="176" rx="10" fill="#fff8ec" stroke="#f0d9a8"/>')
    o.append(f'<text x="696" y="350" font-size="13" font-weight="700" fill="#b36b00">이용자에게 보이는 것</text>')
    o.append(screen(752,420,s["screen"],s.get("scol",RED)))
    o.append(s["who_icon"](836,372))
    o.append(textblock(866,377,s["who"],22,13,"#7a4a00",700,17))
    for i,t in enumerate(s["seq"]):
        y=418+i*26
        o.append(f'<circle cx="846" cy="{y-5}" r="9" fill="#b36b00"/>'+label(846,y-0.5,str(i+1),"#fff",11.5,800))
        o.append(textblock(862,y,t,22,13.5,INK,600,17))
    o.append('</svg>')
    return "".join(o)
one=lambda x,y: person(x,y+4,1,"#7a4a00")
many=lambda x,y: "".join(person(x+dx,y+dy,.7,"#7a4a00") for dx,dy in [(-10,0),(0,-4),(10,0)])
mover=lambda x,y: person(x-6,y+4,.9,"#7a4a00")+arr(x+2,y+2,x+16,y+2,"#7a4a00",2)
S=[
 dict(no=14,name="저녁 러시로 역 주변 기지국이 붐빔",hot=("ENB",),
  steps=[(lambda x,y: tower(x-34,y,AMB,1.3)+crowd(x+26,y+4)+clock(x+60,y-34,AMB),"저녁 러시, 역 주변 셀의 무선 용량이 모자람"),
         (lambda x,y: ppl(x,y,6,INK,22,red=(1,4)),"그 셀에 원래 있던 가입자 일부만 나빠짐 — 같은 사람이 계속"),
         (lambda x,y: chart(x-40,y,"drop",RED,90)+chart(x+60,y,"rise",AMB,90)+label(x-40,y+46,"처리율","#5b6675",12,600)+label(x+60,y+46,"RTT","#5b6675",12,600),
          "내려받기 처리율↓ · RTT↑ (Q-11) — 호·가입자 수와 접속 절차는 평소 그대로")],
  where="역 주변 셀의 무선 쪽(down)이 붐빈다. 장비는 멀쩡하다.",
  screen=["인터넷","느림…"],scol=AMB,who_icon=one,who="역 주변 셀의 일부 이용자 (같은 사람이 계속)",
  seq=["전화·접속은 된다","웹·앱이 느리고 가끔 멈춤","혼잡이 끝나면 돌아옴"]),
 dict(no=15,name="야마노테선에서 4G↔5G 넘어갈 때 잠깐 멈춤",hot=("ENB",),
  steps=[(lambda x,y: train(x-50,y+8)+towers(x+48,y+6,["ok","ok","ok"],30,.8)+f'<line x1="{x-90}" y1="{y+36}" x2="{x+90}" y2="{y+36}" stroke="{GRN}" stroke-width="3"/>',"전철로 노선을 따라 이동하며 셀을 옮김"),
         (lambda x,y: badge(x-34,y,"4G","#5b6675")+arr(x-14,y-6,x+14,y-6,RED,2)+arr(x+14,y+6,x-14,y+6,RED,2)+badge(x+34,y,"5G",RED),"셀을 옮길 때 4G·5G를 오감"),
         (lambda x,y: chart(x,y,"saw",RED,170)+label(x,y+46,"내려받기 — 전환 직후만 멈춤","#5b6675",12,600),"전환 직후 3~8초 내려받기가 멈춤 (Q-08) — 연결은 끊기지 않음")],
  where="노선을 따라 길게 늘어선 셀에서, 셀을 옮기는 세션만.",
  screen=["잠깐","멈춤"],scol=AMB,who_icon=mover,who="노선을 따라 이동 중인 사람만 (머무는 사람은 평소)",
  seq=["역 사이마다 잠깐 멈춤","몇 초 뒤 다시 이어짐","같은 셀에 있으면 평소대로"]),
 dict(no=16,name="5G는 잡히는데 느림 (4G 앵커 붐빔)",hot=("ENB",),
  steps=[(lambda x,y: tower(x-36,y,INK,1.2)+badge(x-36,y+34,"4G","#5b6675")+tower(x+36,y,INK,1.2)+badge(x+36,y+34,"5G","#2f6fd6")+f'<line x1="{x-20}" y1="{y-8}" x2="{x+20}" y2="{y-8}" stroke="#5b6675" stroke-width="2.4" stroke-dasharray="4 3"/>',"5G로 붙은 단말은 4G 기지국(앵커)도 함께 씀"),
         (lambda x,y: tower(x-20,y,AMB,1.3)+badge(x-20,y+34,"4G",AMB)+crowd(x+40,y+4,AMB),"그 4G 앵커 쪽이 붐빔(경합)"),
         (lambda x,y: chart(x,y,"drop",RED,150)+label(x,y+46,"5G 세션 처리율","#5b6675",12,600),"5G 세션의 내려받기 처리율만 떨어짐 (U-15) — 5G 연결은 그대로")],
  where="5G를 쓰려면 4G 기지국(앵커)을 거치는데, 그 4G가 막힌다.",
  screen=["5G","느림…"],scol=AMB,who_icon=one,who="그 지역에서 5G로 붙은 사람만 (4G 이용자는 평소)",
  seq=["5G 표시는 그대로","속도가 계속 안 나옴","그 지역에 있는 동안 계속"]),
 dict(no=17,name="5G 장애로 4G로 몰림",hot=("MME",),warn=("ENB",),
  steps=[(lambda x,y: server(x-20,y)+badge(x-20,y+36,"5G",RED)+x_mark(x+24,y-6,RED,1.4),"5G 단독망(SA) 쪽 장애로 5G에 못 붙음"),
         (lambda x,y: badge(x,y-34,"5G",RED)+"".join(arr(x+dx,y-20,x+dx,y+14,"#5b6675",2) for dx in (-30,0,30))+badge(x,y+34,"4G","#5b6675"),"그 지역 5G 단말이 4G(LTE)로 한꺼번에 내려옴 (TAU·Attach)"),
         (lambda x,y: chart(x,y,"bump",RED,150)+label(x,y+46,"4G 쪽 5G 단말 비율","#5b6675",12,600),"4G에서 5G 단말 비율이 늘어남 (N-01) — 접속 성공·실패는 평소대로"),
         (lambda x,y: badge(x,y+30,"4G","#5b6675")+arr(x,y+16,x,y-18,GRN,2.4)+badge(x,y-32,"5G",GRN),"장애가 끝나면 다시 5G로 돌아감")],
  where="5G 코어가 막혀 5G 단말이 그 지역의 4G로 한꺼번에 내려온다.",
  screen=["5G 안 잡힘","4G 연결"],scol="#5b6675",who_icon=many,who="그 지역의 5G 단말 이용자",
  seq=["5G 표시가 사라짐","4G로는 쓸 수 있음","장애가 끝나면 5G로 돌아옴"]),
 dict(no=18,name="데이터 관문 자원 바닥 (5G UPF)",hot=("SGW",),
  steps=[(lambda x,y: server(x-22,y)+gauge(x+30,y,1.0),"데이터 관문(SGW, 5G에선 UPF)의 세션 자원이 바닥남"),
         (lambda x,y: phone(x-50,y,INK,1)+arr(x-30,y,x+20,y,"#5b6675",2.4)+x_mark(x-4,y-16,RED,1.1)+server(x+46,y)+label(x,y+40,"Cause 73",RED,12.5,700),"새 세션 요청(Attach·Service)을 거절 (S11)"),
         (lambda x,y: server(x,y-26)+"".join(f'<line x1="{x}" y1="{y-10}" x2="{x+dx}" y2="{y+20}" stroke="#c9d1db" stroke-width="2"/>' for dx in (-60,-20,20,60))+towers(x,y+30,["hot","ok","hot","hot"],40,.75),"그 SGW 아래 셀들에 실패가 흩어짐 (C-02↓) — 이미 열린 세션은 그대로")],
  where="새 데이터 연결을 만들어 줄 자리가 없어 요청을 거절한다.",
  screen=["데이터 연결","실패"],who_icon=many,who="그 관문을 지나는 여러 지역에서 새로 접속하는 사람",
  seq=["새로 데이터를 켜면 실패","쓰던 연결은 계속 됨","여러 동네에 흩어져 생김"]),
 dict(no=19,name="접속 관리 장비 과부하가 번짐 (5G AMF)",hot=("MME",),
  steps=[(lambda x,y: server(x-22,y)+gauge(x+30,y,.95),"접속 관리 장비(MME, 5G에선 AMF)에 부하가 몰림"),
         (lambda x,y: clock(x-30,y,RED)+label(x+30,y-4,"접속 3.2배",RED,13,800)+label(x+30,y+16,"코어 2.3배",RED,13,800),"실패하지 않은 호도 접속이 느려짐 (평소 대비)"),
         (lambda x,y: f'<g fill="none" stroke="{RED}" stroke-width="2"><circle cx="{x}" cy="{y}" r="14"/><circle cx="{x}" cy="{y}" r="30" opacity=".6" stroke-dasharray="5 4"/><circle cx="{x}" cy="{y}" r="46" opacity=".35" stroke-dasharray="5 4"/></g>'+label(x,y+5,"25%",RED,12,800),"그 MME 셀의 25%에서 시작해 점점 넓어짐"),
         (lambda x,y: phone(x-30,y,INK,1)+x_mark(x-30,y-2,RED,1)+label(x-30,y+40,"접속 실패",RED,12,700)+phone(x+30,y,INK,1)+f'<path d="M{x+20},{y-4} L{x+28},{y+4} L{x+32},{y-6} L{x+40},{y+2}" fill="none" stroke="{RED}" stroke-width="2.4"/>'+label(x+30,y+40,"연결 뒤 끊김",RED,12,700),"일부는 접속 실패, 일부는 연결 뒤 끊김 (S1AP Cause 500)")],
  where="접속을 처리하는 장비가 밀려, 걸리는 지역이 점점 넓어진다. 다른 MME 는 정상.",
  screen=["연결 중…"],scol=AMB,who_icon=many,who="그 MME가 맡은 지역 (점점 넓어짐)",
  seq=["접속이 늦어짐","일부는 실패하거나 끊김","걸리는 지역이 넓어짐"]),
 dict(no=20,name="음성 통화만 안 걸림 (VoNR·IMS)",hot=("IMS",),
  steps=[(lambda x,y: phone(x-40,y,INK,1.1)+f'<path d="M{x-24},{y-12} L{x+20},{y-26}" stroke="#2f6fd6" stroke-width="2.4"/><path d="M{x-24},{y+8} L{x+20},{y+20}" stroke="{GRN}" stroke-width="2.4"/>'+label(x+44,y-22,"음성(IMS)","#2f6fd6",12,700)+label(x+40,y+26,"데이터",GRN,12,700),"단말은 음성용 연결(IMS)과 데이터 연결을 따로 엶"),
         (lambda x,y: label(x-20,y+8,"☎",INK,30,400)+x_mark(x+24,y,RED,1.3)+label(x,y+42,"ESM 26",RED,12.5,700),"한 PGW 쪽에서 IMS 접속만 거절됨"),
         (lambda x,y: app(x-40,y-6,GRN,"앱")+check(x-40,y+22)+label(x+30,y-2,"C-20 정상",GRN,12.5,700)+label(x+30,y+20,"C-21↓",RED,12.5,700),"호는 성공하고 데이터 접속은 정상 — IMS 접속 성공률만 떨어짐")],
  where="음성 통화용 연결만 막히고 일반 데이터 연결은 정상이다.",
  screen=["전화 걸기","실패"],who_icon=many,who="그 PGW를 지나는 여러 셀의 이용자",
  seq=["인터넷·앱은 된다","음성 통화만 안 걸림","여러 동네에 흩어져 생김"]),
 dict(no=21,name="동영상 서버(CDN) 느림",hot=("SRV",),
  steps=[(lambda x,y: cloud(x-10,y)+clock(x+40,y-16,RED)+label(x,y+40,"망 밖","#5b6675",12,600),"망 밖 동영상 CDN 서버 한 곳이 느리게 답함"),
         (lambda x,y: app(x-40,y,RED,"▶")+app(x,y,GRN)+app(x+40,y,GRN)+check(x,y+30)+check(x+40,y+30)+x_mark(x-40,y+30,RED,.9),"그 동영상 앱 세션만 RTT·재전송↑, 일부 504"),
         (lambda x,y: chart(x,y,"rise",AMB,150)+label(x,y+46,"재시도로 세션 수↑","#5b6675",12,600),"재시도로 세션 수가 오히려 늘어남 (Q-06)")],
  where="망은 정상. 망 밖의 서버 한 곳이 느리다.",
  screen=["동영상","불러오는 중…"],scol=AMB,who_icon=one,who="그 동영상 앱을 쓰는 사람만 (다른 앱은 평소)",
  seq=["영상이 버벅임","끊기고 다시 불러옴","다른 앱은 멀쩡"]),
 dict(no=22,name="결제 앱이 느림 — 주소 찾기(DNS) 지연",hot=("DNS",),
  steps=[(lambda x,y: dns(x-20,y)+clock(x+30,y,RED)+label(x,y+40,"결제 도메인만","#5b6675",12,600),"결제 앱 도메인의 주소 찾기(DNS)가 느려짐 — 일부는 실패"),
         (lambda x,y: phone(x-36,y,INK,1.1)+app(x-36,y-4,"#2f6fd6","¥")+arr(x-14,y,x+30,y,"#5b6675",2,"5 4")+label(x+44,y+5,"?",RED,22,800),"결제 앱이 서버 주소를 늦게 받음"),
         (lambda x,y: chart(x,y,"rise",RED,150)+label(x,y+46,"DNS 응답시간 (U-32)","#5b6675",12,600),"다시 누르며 재시도·끊김 — 다른 도메인·앱은 정상")],
  where="결제 앱 주소를 찾는 단계에서 늦어지고 실패한다.",
  screen=["결제","처리 중…"],scol=AMB,who_icon=one,who="그 SGW를 지나는 셀의 결제 앱 이용자",
  seq=["결제가 늦거나 안 됨","다시 누름","다른 앱은 멀쩡"]),
 dict(no=23,name="행사장 군중으로 사진 올리기 막힘",hot=("ENB",),
  steps=[(lambda x,y: crowd(x-20,y)+tower(x+52,y,AMB,1.1),"행사장에 사람이 모임 (모이는 3분)"),
         (lambda x,y: crowd(x-20,y+10)+upload(x+30,y-14)+upload(x+56,y-6),"공연 20분 동안 다 같이 사진·영상을 올림"),
         (lambda x,y: chart(x,y,"drop",RED,150)+label(x,y+46,"올리기(업링크) 처리율","#5b6675",12,600),"올리기가 먼저 무너짐 (Q-09) — 일부는 100kbps 안팎까지"),
         (lambda x,y: ppl(x,y,4,INK,34)+arr(x-30,y+26,x-60,y+26,"#5b6675",2)+arr(x+30,y+26,x+60,y+26,"#5b6675",2),"흩어지는 2분 — 사람이 빠지면 풀림")],
  where="행사장 기지국의 올리는 쪽(업링크)이 가득 찬다. 접속 실패는 늘지 않는다.",
  screen=["사진 올리기","실패"],who_icon=many,who="행사장 근처에 모인 많은 사람",
  seq=["모이면 느려짐","사진·영상이 안 올라감","흩어지면 풀림"]),
 dict(no=24,name="기지국 증설 뒤 손님이 옮겨 감 (장애 아님)",hot=(),warn=("ENB",),
  steps=[(lambda x,y: tower(x-10,y,RED,1.3)+label(x-10,y+40,"A",RED,13,800)+crowd(x+50,y+4),"처음 30분, A 기지국에 호가 몰려 느려짐"),
         (lambda x,y: tower(x-36,y,RED,1.2)+label(x-36,y+40,"A",RED,13,800)+tower(x+36,y,GRN,1.2)+plus(x+60,y-24)+label(x+36,y+40,"B",GRN,13,800),"30분에 새 기지국 B가 켜짐"),
         (lambda x,y: chart(x,y,"shift",RED,160),"A로 오던 새 호의 60%가 B로 옮겨감 — A 품질은 5분에 걸쳐 회복")],
  where="붐비던 기지국의 손님이 새 기지국으로 옮겨 간다.",
  screen=["빨라짐"],scol=GRN,who_icon=many,who="A 기지국 주변 이용자",
  seq=["처음엔 느림","새 기지국이 켜지면 옮겨 감","점점 빨라짐"]),
 dict(no=25,name="설정 변경 직후 통화 끊김이 계단처럼 늘어남 (SON)",hot=("ENB",),
  steps=[(lambda x,y: gear(x-50,y,RED)+arr(x-30,y,x+6,y,RED,2.4)+tower(x+26,y,RED,1.1)+tower(x+60,y,RED,1.1)+label(x+43,y+40,"1~2곳",RED,12.5,700),"자동 최적화(SON)가 기지국 1~2곳의 핸드오버 설정을 바꿈"),
         (lambda x,y: tower(x-46,y,RED,1.1)+arr(x-28,y-8,x+12,y-24,"#5b6675",2,"5 4")+arr(x-28,y+4,x+12,y+20,"#5b6675",2,"5 4")+tower(x+40,y-20,INK,.9)+tower(x+40,y+22,INK,.9)+x_mark(x-6,y-16,RED,.9)+x_mark(x-6,y+12,RED,.9),"옆 셀(가장 가까운 2곳)로 넘어가던 통화가 실패 (Cause 106)"),
         (lambda x,y: chart(x,y,"step",RED,170)+label(x-30,y+46,"바뀐 시각",RED,12,700)+f'<line x1="{x-8}" y1="{y-30}" x2="{x-8}" y2="{y+30}" stroke="{RED}" stroke-dasharray="3 3"/>',"모든 셀이 같은 분에 한 번에 꺾여 그대로 — 절단률(C-04) 계단")],
  where="그 기지국에서 옆 셀로 넘기는 설정이 바뀐다. 설정값은 데이터에 없다.",
  screen=["통화 끊김"],who_icon=mover,who="그 기지국에서 이동하며 옆 셀로 넘어가는 사람만",
  seq=["넘어가는 순간 끊김","바뀐 시각부터 계속","옮겨 가지 않으면 영향 없음"]),
 dict(no=26,name="SW 업데이트한 특정 기종만 접속 실패",hot=("UE",),
  steps=[(lambda x,y: phone(x-14,y,RED,1.3)+f'<path d="M{x+36},{y+6} a12,12 0 1,1 -4,-10" fill="none" stroke="{RED}" stroke-width="2.6"/>'+label(x,y+46,"SW 버전은 데이터에 없음","#5b6675",12,600),"특정 기종이 소프트웨어를 업데이트"),
         (lambda x,y: phone(x-34,y,RED,1.1)+x_mark(x-34,y-2,RED,1)+phone(x+34,y,INK,1.1)+check(x+34,y-2)+label(x-34,y+40,"그 기종",RED,12,700)+label(x+34,y+40,"다른 기종",GRN,12,700),"그 기종만 기본 베어러를 거절해 등록 실패 (ESM 96·99)"),
         (lambda x,y: server(x,y-26)+"".join(f'<line x1="{x}" y1="{y-10}" x2="{x+dx}" y2="{y+20}" stroke="#c9d1db" stroke-width="2"/>' for dx in (-60,-20,20,60))+towers(x,y+30,["hot","ok","hot","ok"],40,.75),"Service 등 다른 접속도 실패(S1AP 126) — 여러 셀에 흩어짐 (C-02↓)")],
  where="망은 정상. 그 기종만 등록·접속 단계에서 실패한다.",
  screen=["망에","연결 안 됨"],who_icon=one,who="그 기종을 쓰는 사람만 (다른 기종은 같은 곳에서 정상)",
  seq=["업데이트 뒤 망에 안 붙음","데이터·통화 접속 실패","다른 폰은 멀쩡"]),
 dict(no=27,name="지진 — 통화 폭주·기지국 정지",hot=("ENB",),
  steps=[(lambda x,y: rings(x-30,y+6)+f'<path d="M{x+6},{y+26} L{x+70},{y+26}" stroke="#5b6675" stroke-width="2"/>'+label(x+40,y+18,"도쿄","#5b6675",12,700)+label(x-30,y+44,"바다·땅속 40~90km",RED,12,700),"도쿄 밖에서 지진 (규모 4.5~6.2)"),
         (lambda x,y: "".join(phone(x+dx,y,INK,.8) for dx in (-44,-15,14,43))+"".join(arr(x+dx,y-20,x+dx,y-40,RED,2) for dx in (-44,-15,14,43)),"넓은 지역이 진도 3~4로 흔들려 안부 전화가 평소의 수십 배로 몰림 (C-01)"),
         (lambda x,y: towers(x,y,["ok","off","ok","ok"],40,1)+label(x,y+40,"무른 땅 · 진도 5약 이상","#5b6675",12,600),"무른 땅 eNB 일부가 eNB 째 꺼짐 (최대 40셀)"),
         (lambda x,y: tower(x-36,y,"#b4bcc7",1)+x_mark(x-36,y-14,"#8a94a3",.7)+phone(x,y+10,INK,.8)+arr(x+12,y+4,x+36,y-4,GRN,2.4)+tower(x+48,y,GRN,1),"꺼진 셀 이용자는 옆 셀로 옮겨 다시 연결")],
  where="넓은 지역에 안부 통화가 몰린다. 진도 5약 이상인 무른 땅 eNB 일부가 꺼진다(최대 40셀). 상위 장비는 멈추지 않는다.",
  screen=["통화 혼잡","연결 중…"],who_icon=many,who="넓은 지역의 많은 사람 (일부 지역은 셀 정지)",
  seq=["전화가 잘 안 걸림","잠깐 끊겼다 옆 셀로 다시 연결","폭주가 지나면 풀림"]),
]
cards="".join(f'<div class=c>{card(s)}</div>' for s in S)
legend=("<div class=lg><b>읽는 법</b> 위는 <span style='color:%s'>원인 — 무슨 일이 어떻게 벌어지나</span>(①②③ 순서), 아래 왼쪽은 "
        "<span style='color:#5b6675'>망 어디에서</span>(빨간 원이 고장 위치, 주황은 영향만), 오른쪽은 <span style='color:#b36b00'>이용자에게 보이는 것</span>"
        "(화면 · 누가 · 겪는 순서). 맨 아래 왼쪽은 헷갈리기 쉬운 다른 장애, 오른쪽은 시스템의 결론입니다.</div>")%RED
HERE=os.path.dirname(os.path.abspath(__file__))
if len(sys.argv)>1:
  open(sys.argv[1],"w").write("<!doctype html><meta charset=utf-8><title>시나리오 한 장 그림 (14~27)</title>"
   "<style>body{margin:24px;background:#eef1f5;font-family:-apple-system,'Apple SD Gothic Neo',sans-serif}.c{margin:0 0 18px}.lg{margin:0 0 18px;font-size:14px;color:#333}</style>"
   f"<h2>장애 시나리오 한 장 그림 — 원인 → 망 위치 → 이용자 → 결론</h2>{legend}{cards}")
for s in S: open(os.path.join(HERE,f"no{s['no']}.svg"),"w",encoding="utf-8").write(card(s))
