// ── 3D 재생 (사용자 2026-11-25) ─────────────────────────────────────────
//
// **기존 재생 데이터를 3D 로 보여 주기만 한다.** 새로 계산하거나 새 API 를 두지 않는다 —
// `/api/replay/{id}/timeline`·`timelapse`·`route` 응답을 그대로 읽는다(2D 재생과 같은 자료).
// 「평소에는 안 하고 플레이를 누르면」 — 이 파일과 three.js 는 3D 버튼을 누를 때 처음 받는다
// (`map.html` 의 동적 `import`). 평소 지도 로딩에는 들지 않는다.
//
// 2판(사용자 11-25 「이쁘지도 않고 뭘 의미하는지 모르겠다 · 국소 확대라 거기만 문제인 것 같다」):
//   * 카메라: 이 장애의 셀 전체와 노선을 **늘 한 화면에** 두고 비스듬히 공전만 한다. 셀로 파고들지 않는다
//   * 뜻: 아래 자막 한 줄(평소 → 장애 시작 → 첫 이상 → 최대 → 원인 → 판정 끝) · 범례 · 이상 셀 수 추이
//   * 판정이 없는 셀을 「정상」이라 적지 않는다 — timelapse 의 note 그대로 「이상이 잡히지 않은 셀」이다
// 조작(사용자 11-25 「사람이 자유롭게 이동도·리플레이도·멈춤도」): 끌기 회전 · 오른쪽/Shift 끌기 이동 · 휠 확대,
//   진행 막대·추이 그래프로 분 옮기기, ←/→·Space, 속도, 기둥 눌러 그 분 값 보기. 손대면 자동 공전이 멈추고 「⟲ 시점」이 되돌린다
// 모든 장애(사용자 11-25 「미리 만든 시나리오에 데이터만 바꿔 쓰는 것」): 장애마다 만든 것이 없다. 노선·열차는 노선 주입일 때만
// 3판(사용자 11-25 「지도는 흰색인데 3D 는 검정 · 지도에서 바로 이어지게 · 불기둥만으로는 뭔지 모른다」):
//   * 바닥 = **지금 지도 그림 그대로**(`map.html` `rp3dSnap`). 좌표도 지도와 같은 웹 메르카토르라 선이 어긋나지 않는다
//   * 배색을 따른다 — 어두움은 가산 혼합 빛 기둥, 밝음은 진한 불투명 기둥
//   * 전환: 지도 창을 숨기고 → 지금 보기와 같은 중심·축척으로 위에서 내려다본 3D 를 겹쳐 띄우고(교차 페이드) →
//     기울이며 장애 범위로 멀어진다. 닫을 때는 거꾸로 위에서 본 지도 보기로 돌아와 지도로 넘긴다
//   * 글자: 지도와 같은 역·지명(등급별로 가까울 때만) · 이상 셀 이름표(가까운 역 + 지표 값) · 자막에 장소
import * as THREE from './vendor/three/three.module.js';
import { OrbitControls } from './vendor/three/addons/OrbitControls.js';

//: 재생 시간 배분 — 세 구간 A(창 시작~첫 판정 전)·B(첫~마지막 판정 분)·C(판정 끝~마무리)를 비슷한 길이로(사용자 11-25
//: 「뒤가 길고 판정이 너무 짧다」). 구간마다 목표 길이(ms) ÷ 분 수가 그 구간의 한 분 시간이다. **초기값이다**
const SEG_MS = 13000;
//: 한 분 시간 한계(ms) — A·C 는 빈 분이라 빠르게, B 는 0.5배(2.4초/분)보다 빠르지 않게. **초기값이다**
const AC_STEP = [500, 3000], B_STEP = [2400, 4500];
//: 원인 판정이 마지막 판정보다 먼저 났을 때 판정 끝 뒤로 남기는 분 수 — 그 뒤 빈 분은 잘라 낸다. **초기값이다**
const C_TAIL_MIN = 2;
//: 끝마무리 — 가장 넓던 때를 다시 띄워 두는 시간·천천히 멀어지는 시간(ms)·멀어지는 배율. **초기값이다**
const OUT_HOLD_MS = 2500, OUT_PULL_MS = 7000, OUT_ZOOM = 1.4;
//: 이상 셀 이름표·자막 — 한 번 뜨면 최소로 두는 시간(ms) · 이름표를 하나씩 바꾸는 간격(ms). **초기값이다**
const TAG_MIN_MS = 4000, TAG_SWAP_MS = 700, CAP_MIN_MS = 4000;
//: 이름표·표식이 나타나고 사라지는 모양 — 아래에서 올라오며 나타나고, 같은 속도·같은 거리로 내려가며 사라진다(사용자 11-25
//: 「바로 사라지거나 옆으로 가니 버그 같다」). 걸리는 시간(ms)·세로 이동(화면 px)·자리를 옮길 때 미끄러지는 시간(ms)·
//: 자리가 막혀도 그대로 버티는 시간(ms, 공전하며 잠깐 스치는 겹침에 튀지 않게)·자막 세로 이동(px). **초기값이다**
const TAG_FADE_MS = 800, TAG_RISE_PX = 24, TAG_SLIDE_MS = 500, TAG_BLOCK_MS = 400, CAP_RISE_PX = 16;
//: 열차가 역에서 서는 시간(ms). 역 사이는 출발 가속 → 빠르게 → 도착 감속. **초기값이다**
const DWELL_MS = 600;
//: 전환 — 교차 페이드·기울며 멀어지기·닫을 때 위로 돌아오기(ms). **초기값이다**
const XFADE_MS = 450, TILT_MS = 2800, BACK_MS = 3000;
//: 닫을 때 지도로 넘기는 교차 페이드(ms) — 위로 돌아온 뒤(BACK_MS, 사용자 11-25 「3초로 천천히」). **초기값이다**
const CLOSE_XFADE_MS = 500;
//: 판정이 꺼진 기둥이 내려앉는 시간(ms) — 켜질 때(약 0.5초)보다 길게. 판정 칸 경계에서 한꺼번에 꺼지는 것이 「갑자기 낫는다」로
//: 보이지 않게(사용자 11-25). 데이터를 바꾸는 것이 아니라 그림의 속도다. **초기값이다**
const FALL_MS = 4000;
//: 카메라 기울기(도) — 공전. 한 바퀴 시간(ms). 위에서 본 시점은 90° 에서 아주 조금 기울인다(정수직이면 위쪽 방향이 정해지지 않는다)
const EL = 50, EL_TOP = 89.9, LAP_CAM_MS = 60000;
//: 국소 장애(노선이 아니고 셀 범위 반지름이 RMIN 보다 작을 때) — 최소 반경(m)·기둥 크기 하한·기울기를 낮추는 각(도)·
//: 바닥을 따로 촘촘히 찍는 범위(반경의 배수). 더 가까이·더 낮게 비스듬히 본다(사용자 11-25). **초기값이다**
const RMIN_LOCAL = 600, HK_LOCAL = .12, EL_LOCAL_DROP = 10, LOCAL_PLATE_K = 6;
//: 범위 덩어리 — 판정 셀을 RMIN 거리로 덩어리 짓고, 판정 셀-분의 이 몫 이상인 덩어리는 **모두** 범위에 담는다(신주쿠+시부야 같은
//: 두 곳 장애, 사용자 11-25). 그보다 작은 외톨이(1분만 켜진 먼 셀 등)만 「화면 밖」으로 뺀다. **초기값이다**
const CLUSTER_SHARE = .15;
//: 몫이 작아도 셀마다 평균 이 분 수 이상 판정이 이어진 덩어리는 담는다 — 「1~2분만 켜진 외톨이」와 가르는 선(F-611 시부야 12% · 셀당 10.6분). **초기값이다**
const CLUSTER_MIN_MIN = 3;
//: 기둥이 멀리서도 보이게 — 공전 시점 화면 높이에 대한 최소 높이(나빠짐 기둥 기준)·최소 굵기(심 반지름) 비율. **초기값이다**
const PILLAR_MIN_SCR = .06, CORE_MIN_SCR = .0025;
//: 장애 종류 표식 — 크기(범위 반경의 비율, m 한계)·흔들림 주기(ms). **초기값이다**
const MARK_K = .038, MARK_M = [25, 300], MARK_BOB_MS = 3200;
//: 셀 장애(노선이 아닌 것, 국소 포함)의 공전 시점 — 기울기(도)·거리 배율. 더 낮게, 더 멀리서 비스듬히(사용자 11-25 「두 번째 화면처럼」).
//: 바닥 범위(범위 반경의 배수)도 지평선 쪽까지 넓히고, 먼 바닥은 배경색 안개로 흐린다(시작~끝 거리 = 공전 거리의 배수). **초기값이다**
const EL_CELL = 30, DIST_CELL_K = 1.35, G_CELL_K = 5, FOG_K = [3.5, 12];
//: 높은 건물·역 건물(사용자 11-25 「지도만 보이니 허전하다」) — 높이 배율(실제 m 에 곱한다)·기둥 대비 상한(이 높이의 건물이
//: 나빠짐 기둥의 몇 배까지, 0 = 상한 없음)·그 기준 높이(m)·역 띠를 세우는 높이(m)·이름표를 다는 높이(m 이상)·한 화면 이름표 수. **초기값이다**
//: 실제 축척(배율 1·상한 없음) — 사용자 11-25 「실제처럼 높여 봐」. 앞 값은 배율 2.5·상한 .8 이었다(비교 캡처 3d16_611_now)
const BLDG_K = 1, BLDG_PILLAR = 0, BLDG_REF_H = 250, STN_H = 12, BLDG_LB_H = 150, BLDG_LB_N = 4;
//: 건물을 만들 때 한 번에 메인 스레드를 쥐는 시간(ms) — 넘으면 한 프레임 쉬고 잇는다(화면 멈칫 방지). **초기값이다**
const BLDG_SLICE_MS = 12;
//: 낮은 건물 칸 — 그리는 반경 = 화면 가운데 축척 막대 값 × 이 배수(사용자 11-25 「1km 축척일 때 5km」)·칸을 다시 고르는 간격(ms)·
//: 한 번에 받는 칸 수(가까운 칸부터). **초기값이다**
const BLDG_VIEW_K = 5, BLDG_TICK_MS = 400, BLDG_BATCH = 16;
//: 기둥 높이(m) — 나빠짐·심각. 보이게 하려는 과장이다. **초기값이다**
const H_DEG = 1300, H_SEV = 2400;
//: 기둥이 눈에 보이는 꼭대기(기둥 높이의 비율) — 어두운 배색은 위로 갈수록 (1−h)^1.7 로 투명해져 절반 위는 0.3 아래로 옅다(밝은 배색은 끝까지 0.2).
//: 표식·이름표를 이 높이에 붙인다 — 기둥 끝(c.h)에 붙이면 빈 허공에 떠 보였다(사용자 11-25). **초기값이다**
const BEAM_VIS = [.5, .9];
//: 기둥 굵기(m) — 심·후광. 바닥 번짐 지름(m) — 나빠짐·심각. **초기값이다**
const CORE_R = 38, HALO_R = 130, GLOW_DEG = 750, GLOW_SEV = 1100;
//: 화면 범위 최소 반경(m) · 후광을 그리는 셀 수 한계(넘으면 심만 — 그리기 수를 줄인다). **초기값이다**
const RMIN = 1500, HALO_MAX = 400;
//: 바닥 번짐을 옅게 하기 시작하는 셀 수. **초기값이다**
const GLOW_N = 150;
//: 열차 — 대수·한 바퀴 시간(ms, 화면 시간)·꼬리 길이(점 수·간격 m). 실제 운행이 아니라 「열차가 지나며 전환한다」를 보이는 그림. **초기값이다**
const TRAINS = 8, LAP_MS = 90000, TAIL_N = 12, TAIL_GAP = 70;
//: 열차가 이 거리(m) 안을 지나면 판정이 켜진 기둥이 잠깐 밝아진다. **초기값이다**
const FLASH_M = 350;
//: 바닥 그림 해상도(px, 긴 변) — 넓은 바닥 · 지금 보기(화면의 2배까지). **초기값이다**
const WIDE_PX = 4096, NEAR_PX = 4096;
//: 글자 — 이 화면 축척(m/px)보다 가까울 때만: 2등급 역·3등급 역·지명. 지도(`ST_T2_M`·`ST_T3_M` 막대 100px 안팎)에 맞춘 값. **초기값이다**
const T2_MPP = 20, T3_MPP = 10, AREA_MPP = 60;
//: 이상 셀 이름표 수 · 「○○역 부근」으로 부를 수 있는 거리(m) — 넘으면 지명으로. **초기값이다**
const CELL_TAGS = 4, NEAR_STN_M = 1500;
//: 가까운 역을 고를 때 2등급·3등급 역에 더하는 거리(m) — 큰 역을 앞세운다. **초기값이다**
const STN_PEN = [150, 500];

const T = (s, a) => (typeof window.T === 'function' ? window.T(s, a) : s);
const ms = v => Date.parse(String(v || '').replace(' ', 'T'));
const hhmm = v => String(v || '').slice(11, 16);
const ease = t => t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
const RAD = Math.PI / 180;
// 지도와 같은 투영(`map.html` mx·my) — 경도 그대로, 위도는 메르카토르(도 단위)
const MY = lat => (180 / Math.PI) * Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360));
//: 재생 속도 고르기 · 누르기로 보는 움직임 한계(px) — 이보다 많이 움직이면 끌기다. **초기값이다**
const SPEEDS = [.5, 1, 2, 4], CLICK_PX = 5;

// 3D 화면 전용 모양 — 지도 CSS(`.r3d*`)에 얹는다. 열 때 넣고 닫을 때 걷는다. 상자 색은 배색 변수로
const CSS = `
.r3d{--bx:rgba(10,14,20,.82);--bd:#263142;--fg:#d7dee7;--fg2:#aab6c5;--cap:rgba(5,8,13,.72);--sh:#000;
  transition:background-color .45s}
.r3d.lt{--bx:rgba(255,255,255,.92);--bd:#d1d5db;--fg:#1f2937;--fg2:#4b5563;--cap:rgba(255,255,255,.88);--sh:#fff}
.r3d>*{transition:opacity .45s}
.r3d.pre{background:transparent!important}.r3d.pre>*:not(.r3d-msg){opacity:0}
.r3d.pre .r3d-msg{background:var(--bx);color:var(--fg);padding:8px 14px;border-radius:8px;border:1px solid var(--bd)}
.r3d-msg{color:var(--fg)}
.r3d.lt .r3d-hud{background:rgba(255,255,255,.94);color:#111827;border-color:#d1d5db}
.r3d.lt .r3d-hud button{background:#f3f4f6;color:#111827;border-color:#d1d5db}
.r3d.lt .r3d-hud .r3d-name{color:#b45309}.r3d.lt .r3d-bar{background:#d1d5db}
.r3d-cap{position:absolute;left:50%;bottom:34px;transform:translateX(-50%);max-width:min(900px,80%);text-align:center;pointer-events:none}
.r3d-cap b{display:inline-block;color:var(--fg);font-size:22px;font-weight:700;line-height:1.35;padding:8px 18px;border-radius:10px;
  background:var(--cap);text-shadow:0 1px 3px var(--sh);transition:opacity .45s,transform .45s}
.r3d-cap small{display:block;margin-top:6px;color:var(--fg2);font-size:14px;text-shadow:0 1px 2px var(--sh),0 0 4px var(--sh);transition:opacity .45s,transform .45s}
.r3d-cap.fade b,.r3d-cap.fade small{opacity:0;transform:translateY(${CAP_RISE_PX}px)}
.r3d-leg{position:absolute;right:14px;bottom:14px;background:var(--bx);border:1px solid var(--bd);border-radius:8px;
  padding:8px 11px;color:var(--fg);font-size:12px;line-height:1.75;pointer-events:none}
.r3d-leg i{display:inline-block;vertical-align:middle;margin-right:7px}
.r3d-leg .bm{width:4px;height:15px;border-radius:2px;background:linear-gradient(#fff0,#e6edf3)}
.r3d.lt .r3d-leg .bm{background:linear-gradient(#dc262633,#dc2626)}
.r3d-leg .sv{width:10px;height:10px;border-radius:50%;background:#ff4d4d;box-shadow:0 0 8px #ff4d4d}
.r3d-leg .dg{width:10px;height:10px;border-radius:50%;background:#ffc23d;box-shadow:0 0 8px #ffc23d}
.r3d.lt .r3d-leg .sv{background:#dc2626;box-shadow:none}.r3d.lt .r3d-leg .dg{background:#d97706;box-shadow:none}
.r3d-leg .pt{width:4px;height:4px;border-radius:50%;background:#7d8ea6;margin:0 10px 0 3px}
.r3d-leg .n{color:#f59e0b;margin-top:2px}
.r3d-leg .tr{width:14px;height:4px;border-radius:2px;background:linear-gradient(90deg,#b6ff7a00,#d9ffb0)}
.r3d.lt .r3d-leg .tr{background:linear-gradient(90deg,#3f7a1000,#3f7a10)}
.r3d-chart{position:absolute;left:14px;bottom:14px;width:300px;background:var(--bx);border:1px solid var(--bd);border-radius:8px;
  padding:7px 9px 5px;color:var(--fg2);font-size:11px;pointer-events:none}
.r3d-chart canvas{position:static;display:block;width:100%;height:92px;pointer-events:auto;cursor:pointer}
.r3d-lbs,.r3d-pins{position:absolute;inset:0;pointer-events:none;overflow:hidden}
.r3d-lb{position:absolute;left:0;top:0;white-space:nowrap;font-size:12px;color:#c9d1d9;
  text-shadow:0 0 3px var(--sh),0 0 2px var(--sh),0 0 1px var(--sh)}
.r3d-lb.s1{color:#b5e07a;font-weight:700}.r3d-lb.s2{color:#c9d1d9;font-weight:600}
.r3d-lb.a{color:#8b97a6;font-size:13px;letter-spacing:.06em;opacity:.8}.r3d-lb.a.o{opacity:.5}
.r3d-lb.b{color:#8b97a6;font-size:11px;opacity:.7}.r3d.lt .r3d-lb.b{color:#6b7280}
.r3d.lt .r3d-lb.s1{color:#3f7a10}.r3d.lt .r3d-lb.s2{color:#374151}.r3d.lt .r3d-lb.a{color:#6b7280}
.r3d-tag{position:absolute;left:0;top:0;color:#bfe89a;font-size:12px;font-weight:600;letter-spacing:.05em;pointer-events:none;
  text-shadow:0 0 6px var(--sh),0 0 2px var(--sh);white-space:nowrap}
.r3d.lt .r3d-tag{color:#3f7a10}
.r3d-tag.fa{z-index:3;color:#fff;background:rgba(220,38,38,.9);padding:2px 8px;border-radius:5px;text-shadow:none;pointer-events:auto;cursor:pointer}
.r3d-tag.cl{color:var(--fg);background:var(--bx);border:1px solid var(--bd);border-left:4px solid #ff4d4d;padding:3px 8px;border-radius:5px;
  font-weight:500;letter-spacing:0;text-shadow:none;pointer-events:auto;cursor:pointer;font-size:12px}
.r3d-tag.cl.d{border-left-color:#ffc23d}.r3d-tag.cl b{font-weight:700}
/* 셀 이름표의 나타남·사라짐·미끄러짐은 JS 가 프레임마다 정한다(place) */
.r3d canvas.r3d-gl{cursor:grab}.r3d canvas.r3d-gl:active{cursor:grabbing}
.r3d-bar{top:50px;height:7px;cursor:pointer}.r3d-bar:hover{height:9px;top:49px}.r3d-bar i{transition:none}
.r3d-hud .r3d-sp{display:flex;gap:2px}.r3d-hud .r3d-sp button{padding:3px 7px;font-size:12px}
.r3d-hud .r3d-sp button.on{background:#ffb86b;color:#1b1206;border-color:#ffb86b}
.r3d-tip{position:absolute;left:0;top:0;min-width:230px;max-width:330px;background:var(--bx);border:1px solid var(--bd);border-radius:8px;
  padding:8px 11px;color:var(--fg);font-size:12px;line-height:1.6;box-shadow:0 4px 18px #0006;pointer-events:auto}
.r3d-tip b{display:block;font-size:13px;margin-bottom:3px;padding-right:18px}
.r3d-tip .x{position:absolute;right:6px;top:4px;cursor:pointer;color:var(--fg2)}
.r3d-tip .s{color:#ef4444}.r3d-tip .d{color:#d97706}.r3d-tip .n{color:var(--fg2)}
.r3d-tip td{padding:0 10px 0 0;vertical-align:top}.r3d-tip td:first-child{color:var(--fg2);white-space:nowrap}
.r3d-tip p{margin:4px 0 0;color:var(--fg2)}
.r3d-note{position:absolute;top:66px;left:50%;transform:translateX(-50%);max-width:80%;background:var(--bx);border:1px solid #f59e0b;
  border-left:4px solid #f59e0b;color:var(--fg);padding:6px 14px;border-radius:8px;font-size:13px;pointer-events:none;text-align:center}
.r3d-chart .k{opacity:.8}
.r3d,.r3d *{-webkit-user-select:none;user-select:none}
.r3d-hint{position:absolute;left:50%;bottom:118px;transform:translateX(-50%);pointer-events:none;text-align:center;opacity:0;transition:opacity 1.2s}
.r3d-hint.on{opacity:1}
.r3d-hint>div{animation:r3dPulse 2.6s ease-in-out infinite;color:var(--fg);background:var(--cap);border:1px solid var(--bd);border-radius:14px;padding:8px 16px 7px;display:flex;align-items:center;gap:10px}
.r3d-hint svg{display:block;width:40px!important;height:40px!important;flex:none}.r3d-hint span{white-space:nowrap}.r3d-hint span{font-size:14px;font-weight:600}
@keyframes r3dPulse{0%,100%{opacity:.45}50%{opacity:.95}}
.r3d-leg .mk{width:11px;height:11px;border-radius:50%;background:#ff4d4d;border:2px solid #fff;box-sizing:border-box}
`;

let CUR = null;   // 열린 3D 재생 하나
// 3D 만 받는 레이어 — 한 번 받아 둔다(지도 첫 로딩에 들지 않는다). 실패하면 다음에 다시 받는다
const LAYER_C = {};
// 낮은 건물 칸 색인 — 없거나 비면 null(높은 건물만 그린다)
let BIDX = null;
const bldgIndex = () => BIDX || (BIDX = fetch('/api/map/bldg/index').then(r => r.ok ? r.json() : null)
  .then(j => j && j.cells && Object.keys(j.cells).length ? j : null).catch(() => { BIDX = null; return null; }));
//: JIS 3차 메시(약 1km) 코드 — graphio `geo_coverage.mesh_codes` 의 `MESH1K` 와 같은 식(초 단위 정수 나눗셈)
const meshCode = (lon, lat) => {
  const ls = lat * 3600, os = (lon - 100) * 3600;
  return '' + Math.floor(ls / 2400) + Math.floor(os / 3600) + Math.floor((ls % 2400) / 300) + Math.floor((os % 3600) / 450)
    + Math.floor((ls % 300) / 30) + Math.floor((os % 450) / 45);
};
const layerOf = name => LAYER_C[name] || (LAYER_C[name] = fetch(`/api/map/layer/${name}`).then(r => r.json()).then(j => j.items || [])
  .catch(() => { delete LAYER_C[name]; return []; }));

export function isOpen() { return !!CUR; }
export function close() { if (CUR) CUR.close(); }

export async function open(opt) {
  close();
  const { fid, host, title } = opt;
  const light = opt.theme === 'light';
  const panels = st => { try { opt.panels && opt.panels(st); } catch (e) { /* 지도 쪽 사정 — 3D 는 계속 */ } };
  const style = document.createElement('style'); style.textContent = CSS; document.head.appendChild(style);
  const wrap = document.createElement('div');
  wrap.className = 'r3d pre' + (light ? ' lt' : '');
  const mapBg = getComputedStyle(host).backgroundColor || (light ? '#e9eaec' : '#0d1117');
  wrap.style.background = mapBg;
  wrap.innerHTML = `<div class=r3d-hud><b>${esc(T('3D 재생'))} · F-${fid}</b><span class=r3d-name>${esc(title || '')}</span>`
    + `<span class=r3d-t>--:--</span><span class=r3d-cnt></span><i></i>`
    + `<button class=r3d-home title="${esc(T('처음부터'))}">⏮</button><button class=r3d-play title="${esc(T('재생 · 일시정지 (Space)'))}">❚❚</button>`
    + `<span class=r3d-sp>${SPEEDS.map(v => `<button data-v=${v}${v === 1 ? ' class=on' : ''}>${v}×</button>`).join('')}</span>`
    + `<button class=r3d-view title="${esc(T('처음 전체 보기로 돌아가고 자동으로 돕니다'))}">⟲ ${esc(T('시점'))}</button>`
    + `<button class=r3d-x title="${esc(T('닫기 (Esc)'))}">✕</button></div>`
    + `<div class=r3d-bar title="${esc(T('누르거나 끌어 그 분으로 갑니다 (←/→ 1분)'))}"><i></i></div>`
    + `<div class=r3d-lbs></div><div class=r3d-pins></div>`
    + `<div class=r3d-chart><div>${esc(T('이상 셀 수'))}</div><canvas></canvas></div>`
    + `<div class=r3d-leg><div><i class=bm></i>${esc(T(light ? '기둥 = 이상이 잡힌 셀' : '빛 기둥 = 이상이 잡힌 셀'))}</div>`
    + `<div><i class=sv></i>${esc(T('빨강 = 심각'))} &nbsp; <i class=dg></i>${esc(T('노랑 = 나빠짐'))}</div>`
    + `<div><i class=pt></i>${esc(T('점 = 이상이 잡히지 않은 셀'))}</div>`
    + `<div><i class=tr></i>${esc(T(light ? '초록 점 = 야마노테선 열차' : '빛 덩어리 = 야마노테선 열차'))}</div></div>`
    + `<div class=r3d-cap><b></b><small></small></div>`
    + `<div class=r3d-msg>${esc(T('불러오는 중…'))}</div>`;
  host.appendChild(wrap);
  // 3D 화면 위 휠·누름은 뒤의 지도로 가지 않게(지도가 확대·끌기 된다) — 페이드 중(pre)에도 막는다: 바닥 그림을 찍은 뒤 지도가 움직이면 안 된다
  let onTouch = () => {};   // 끝난 뒤 조작 안내 — 손대면 사라진다(장면이 선 뒤에 채운다)
  // 끌어서 돌릴 때 글자가 파랗게 선택되지 않게(사용자 11-25)
  wrap.addEventListener('selectstart', e => e.preventDefault());
  wrap.addEventListener('wheel', e => { e.preventDefault(); e.stopPropagation(); onTouch(); }, { passive: false });
  for (const t of ['mousedown', 'pointerdown', 'click', 'dblclick', 'contextmenu']) wrap.addEventListener(t, e => e.stopPropagation());
  const $ = s => wrap.querySelector(s);
  // 지도 창부터 서서히 숨긴다. 지금 보기 그림은 **지도를 건드리기 전에** 찍는다(셀·글자는 빠진 배경만)
  panels('on');
  const view = opt.view && opt.view.w > 0 ? opt.view : null;
  const nearP = view && opt.snap ? opt.snap(view, Math.min(NEAR_PX, 2 * Math.max(host.clientWidth, host.clientHeight))).catch(() => null) : Promise.resolve(null);

  let alive = true, ready = false, closing = null;
  // 키 — 3D 가 열려 있는 동안은 먼저 받아 뒤의 지도로 넘기지 않는다(캡처 단계)
  let keys = null;
  const onKey = e => {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); st.close(); return; }
    if (keys && keys(e)) { e.preventDefault(); e.stopPropagation(); }
  };
  let teardown = () => {};
  const finish = () => { teardown(); wrap.remove(); style.remove(); panels('off'); if (CUR === st) CUR = null; };
  // 자료를 받기 전에 닫아도 된다 — 그때는 틀만 걷는다. 장면이 서 있으면 위에서 본 지도 보기로 돌아간 뒤 넘긴다
  const st = { close: () => {
    if (!alive) return; alive = false; window.removeEventListener('keydown', onKey, true);
    if (ready && closing) closing(); else finish(); } };
  CUR = st;
  $('.r3d-x').onclick = () => st.close();
  window.addEventListener('keydown', onKey, true);
  const showMsg = t => { $('.r3d-msg').textContent = t; wrap.classList.remove('pre'); };

  let data;
  try {
    const get = p => fetch(`/api/replay/${fid}/${p}`).then(r => r.json());
    const [tl, ta, rt] = await Promise.all([get('timeline'), get('timelapse'), get('route')]);
    if (ta.error || !ta.frames || !ta.frames.length) throw new Error(ta.error || T('재생할 프레임이 없습니다'));
    data = { tl, ta, rt };
  } catch (e) {
    if (alive) showMsg(T('3D 재생 자료를 받지 못했습니다') + ` — ${e.message || e}`);
    return st;
  }
  if (!alive) return st;
  // 셀 판정이 한 분도 없는 장애 — 노선 주입이면 노선·열차·주입 구간을 그대로 보여 주고 「잡히지 않음」을 알린다(사용자 11-25
  // 「야마노테선 장애는 전부 보여야」). 노선이 아니면(DNS·APN 처럼 셀이 아닌 축) 그릴 것이 없다고 말하고 끝낸다.
  // 판정 기록이 보관 기간을 지나 지워진 것(`expired`)과 탐지가 놓친 것은 다른 일이다 — 문구를 섞지 않는다
  const lit = new Set(); for (const f of data.ta.frames) for (const k in (f.cells || {})) if (f.cells[k] && f.cells[k].state) lit.add(k);
  const noCell = !lit.size, expired = !!data.ta.expired;
  const dt16 = v => String(v || '').slice(5, 16).replace('T', ' ');
  const expMsg = T('이 장애의 판정 기록은 보관 기간이 지나 셀 그림이 없습니다 (남아 있는 기록: {e}부터)', { e: dt16(data.ta.anomaly_earliest) });
  const routeOk = /^ROUTE/i.test(String(opt.target || '')) && !!(data.rt.track && data.rt.track.ring && data.rt.track.ring.length);
  if (noCell && !routeOk) { for (const s of ['.r3d-chart', '.r3d-leg', '.r3d-cap', '.r3d-bar']) $(s).remove(); showMsg(expired ? expMsg : T('이 장애는 셀 판정이 없어 3D로 보여 줄 것이 없습니다')); return st; }

  let renderer;
  try { renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true }); }
  catch (e) { showMsg(T('3D 화면을 열 수 없습니다(WebGL 없음)')); return st; }
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  renderer.domElement.className = 'r3d-gl';
  wrap.insertBefore(renderer.domElement, wrap.firstChild);
  const SEV = new THREE.Color(light ? 0xdc2626 : 0xff4d4d), DEG = new THREE.Color(light ? 0xe08a00 : 0xffc23d), FLASH = new THREE.Color(0xffffff);

  // ── 좌표 — 이 장애의 셀 + 노선을 감싸는 범위 가운데를 원점(m). x 동쪽, z 남쪽, y 위. 지도와 같은 메르카토르 ─────
  const { tl, ta, rt } = data;
  const frames = ta.frames;
  // 노선·열차는 **노선에 주입한 장애**(target `ROUTE…`)에만 그린다. `route` API 는 장애 셀과 가장 많이 겹치는 노선을
  // 종류와 상관없이 고르므로(DNS F-597 도 YMT) 그것만 보면 엉뚱한 장애에 열차가 돈다
  const onRoute = /^ROUTE/i.test(String(opt.target || '')) && !!(rt.track && rt.track.ring && rt.track.ring.length);
  const ring = onRoute ? rt.track.ring : [];
  const lineName = onRoute && rt.routes && rt.routes[0] ? rt.routes[0].name : '';
  const fc = ta.cells.filter(c => lit.has(c.key));
  // 범위 — 지도 「전체 보기」와 같은 방식: 1~99% 분위 띠(폭만큼 여유) 밖의 동떨어진 셀은 범위에서 뺀다(그리기는 한다)
  const band = v => { const a = [...v].sort((p, q) => p - q), q0 = a[Math.floor(a.length * .01)], q1 = a[Math.ceil(a.length * .99) - 1], w = (q1 - q0) || .01; return [q0 - w, q1 + w]; };
  const [ox0, ox1] = fc.length ? band(fc.map(c => c.lon)) : [0, 0], [oy0, oy1] = fc.length ? band(fc.map(c => c.lat)) : [0, 0];
  let inSpan = fc.filter(c => c.lon >= ox0 && c.lon <= ox1 && c.lat >= oy0 && c.lat <= oy1);
  // 덩어리 — 셀이 적으면 분위 띠가 동떨어진 셀을 못 거른다(F-583: 가미나카자토 12셀 + 1분만 켜진 아카사카 4셀). 판정 셀-분이 많은
  // 셀부터 RMIN 안의 덩어리에 붙이고, 셀-분 몫이 CLUSTER_SHARE 이상인 덩어리를 모두 범위로 삼는다(F-611 신주쿠+시부야 둘 다)
  const clOf = new Map(); let clKeep = [];
  if (!(/^ROUTE/i.test(String(opt.target || ''))) && fc.length) {
    const w = new Map(); for (const f of data.ta.frames) for (const k in (f.cells || {})) if (f.cells[k] && f.cells[k].state) w.set(k, (w.get(k) || 0) + 1);
    const kx = 111320 * Math.cos(fc[0].lat * RAD), dm = (a, b) => Math.hypot((a.lon - b.lon) * kx, (a.lat - b.lat) * 111320);
    const cls = [];
    for (const c of [...fc].sort((a, b) => (w.get(b.key) || 0) - (w.get(a.key) || 0))) {
      let k = cls.find(g => dm(g.seed, c) <= RMIN);
      if (!k) { k = { seed: c, cells: [], w: 0 }; cls.push(k); }
      k.cells.push(c); k.w += w.get(c.key) || 0;
    }
    const tot = cls.reduce((x, g) => x + g.w, 0);
    clKeep = cls.filter(g => g.w >= tot * CLUSTER_SHARE || g.w / g.cells.length >= CLUSTER_MIN_MIN).sort((a, b) => b.w - a.w);
    clKeep.forEach((g, i) => { for (const c of g.cells) clOf.set(c.key, i); });
    if (clKeep.length) inSpan = clKeep.flatMap(g => g.cells);
  }
  const outN = fc.length - inSpan.length;
  const span = [...inSpan.map(c => [c.lon, c.lat]), ...ring];
  let bx0 = Infinity, bx1 = -Infinity, by0 = Infinity, by1 = -Infinity;
  for (const [x, y] of span) { bx0 = Math.min(bx0, x); bx1 = Math.max(bx1, x); by0 = Math.min(by0, y); by1 = Math.max(by1, y); }
  const lon0 = (bx0 + bx1) / 2, lat0 = (by0 + by1) / 2, my0 = MY(lat0);
  // 메르카토르 한 단위(도)가 이 위도에서 몇 m 인가 — 경도·메르카토르 y 둘 다 같은 배율이다
  const KX = 111320 * Math.cos(lat0 * RAD);
  const P = (lon, lat) => [(lon - lon0) * KX, (my0 - MY(lat)) * KX];
  const S = (x, y) => [(x - lon0) * KX, (y + my0) * KX];      // 지도 SVG 좌표(x=경도, y=−메르카토르) → m
  // 범위 반지름 — 카메라가 어느 쪽에서 보든 이 원이 화면에 들어오게 거리를 정한다
  // 셀이 한곳에 몰려도 너무 파고들지 않게 최소 반경(RMIN)을 둔다. 기둥 크기는 범위에 맞춰 줄인다(야마노테 고리 ≈ 1)
  const R0 = Math.max(0, ...span.map(([x, y]) => Math.hypot(...P(x, y))));
  // 국소 장애 — 노선이 아니고 셀이 좁게 몰렸으면 더 가까이(최소 반경을 줄이고), 기둥도 그 범위에 맞게 줄인다
  const local = !onRoute && R0 < RMIN;
  const R = local ? Math.max(RMIN_LOCAL, R0) : Math.max(RMIN, R0);
  const hk = Math.min(1, Math.max(local ? HK_LOCAL : .3, R / 6500));
  // 노선 장면은 그대로(사용자 「이대로 좋다」). 셀 장애는 낮게·멀리
  const elRun = onRoute ? EL : EL_CELL;
  const HALO = fc.length <= HALO_MAX;
  // 셀이 수백 개 겹치면 가산 혼합 번짐이 하얗게 뭉갠다(F-593 1,264셀) — 셀 수에 따라 옅게
  const gk = Math.min(1, Math.sqrt(GLOW_N / Math.max(1, fc.length)));

  // 지금 지도 보기 — 가운데(m)·세로 길이(m). 없으면(옛 지도) 범위를 위에서 본 것으로
  const mapC = view ? S(view.x + view.w / 2, view.y + view.h / 2) : [0, 0];
  const mapH = view ? view.h * KX : R * 2.4;
  const mapW = view ? view.w * KX : R * 2.4;
  // 바닥 — 범위의 2.6배, 그리고 지금 지도 보기가 다 들어오게
  const G = Math.max(R * (onRoute ? 2.6 : G_CELL_K), (Math.abs(mapC[0]) + mapW / 2) * 1.1, (Math.abs(mapC[1]) + mapH / 2) * 1.1);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(mapBg);
  // 먼 바닥을 배경색으로 흐린다 — 지도와 맞춘 시점(top)에서는 끄고, 기울며 켜고, 닫을 때 다시 끈다(fogK)
  const fog = onRoute ? null : new THREE.Fog(mapBg, 1e9, 2e9); if (fog) scene.fog = fog;
  const cam = new THREE.PerspectiveCamera(40, 1, 20, Math.max(R * 20, mapH * 6));
  const disposables = [];
  const keep = o => (disposables.push(o), o);
  const texOf = cv => { const t = keep(new THREE.CanvasTexture(cv)); t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = renderer.capabilities.getMaxAnisotropy(); return t; };
  // 바닥 판 — 깊이를 쓰지 않고 맨 먼저 그린다(두 장이 겹쳐도 깜박이지 않게, 기둥·점이 늘 위에)
  const plate = (w, h, x, z, order) => {
    const m = new THREE.Mesh(keep(new THREE.PlaneGeometry(w, h)), keep(new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, depthTest: false, depthWrite: false })));
    m.rotation.x = -Math.PI / 2; m.position.set(x, 0, z); m.renderOrder = order; scene.add(m); return m;
  };
  const wide = plate(G * 2, G * 2, 0, 0, -3); wide.visible = false;
  const nearCv = await nearP;
  if (!alive) { renderer.dispose(); return st; }
  const near = nearCv ? plate(mapW, mapH, mapC[0], mapC[1], -2) : null;
  if (near) near.material.map = texOf(nearCv);

  // 둥근 빛 무늬 — 바닥 번짐·열차가 같이 쓴다
  const gc = document.createElement('canvas'); gc.width = gc.height = 128;
  const gg = gc.getContext('2d'), rg = gg.createRadialGradient(64, 64, 0, 64, 64, 64);
  rg.addColorStop(0, 'rgba(255,255,255,1)'); rg.addColorStop(.18, 'rgba(255,255,255,.55)'); rg.addColorStop(.5, 'rgba(255,255,255,.12)'); rg.addColorStop(1, 'rgba(255,255,255,0)');
  gg.fillStyle = rg; gg.fillRect(0, 0, 128, 128);
  const glowTex = keep(new THREE.CanvasTexture(gc));

  // ── 옅은 점 — 이상이 잡히지 않은 셀(생성 범위 셀 전체). 지도의 셀 점 자리 그대로 ─────────────
  const dotPos = [];
  for (const c of opt.cells || ta.cells) {
    const [x, z] = P(c.lon, c.lat); if (Math.abs(x) > G || Math.abs(z) > G) continue;
    dotPos.push(x, 4, z);
  }
  const dotGeo = keep(new THREE.BufferGeometry()); dotGeo.setAttribute('position', new THREE.Float32BufferAttribute(dotPos, 3));
  scene.add(new THREE.Points(dotGeo, keep(new THREE.PointsMaterial({ color: light ? 0x6b7280 : 0x7d8ea6, size: 2.2, sizeAttenuation: false, transparent: true, opacity: light ? .55 : .5, depthWrite: false }))));

  // ── 이상 셀 — 기둥(심 + 후광) + 바닥 번짐 ─────────────────────────────────
  // 어두움: 아래가 밝고 위로 갈수록 투명한 빛(가산 혼합). 밝음: 가산 혼합은 흰 바탕에서 사라지므로 진한 색을 일반 혼합으로
  // 원기둥 uv.y 는 바닥 0 ·꼭대기 1 이다
  const beamMat = () => keep(new THREE.ShaderMaterial({
    uniforms: { uColor: { value: new THREE.Color() }, uI: { value: 0 } },
    vertexShader: 'varying float vH; void main(){ vH=uv.y; gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.); }',
    fragmentShader: light
      ? 'uniform vec3 uColor; uniform float uI; varying float vH; void main(){ float a=clamp(mix(.95,.2,vH)*uI,0.,1.); gl_FragColor=vec4(uColor,a); }'
      : 'uniform vec3 uColor; uniform float uI; varying float vH; void main(){ float a=pow(1.-vH,1.7)*uI; gl_FragColor=vec4(uColor*a,a); }',
    transparent: true, depthWrite: false, blending: light ? THREE.NormalBlending : THREE.AdditiveBlending, side: THREE.DoubleSide }));
  const coreGeo = keep(new THREE.CylinderGeometry(CORE_R * hk, CORE_R * hk, 1, 12, 1, true)); coreGeo.translate(0, .5, 0);
  const haloGeo = keep(new THREE.CylinderGeometry(HALO_R * hk, HALO_R * hk * .4, 1, 16, 1, true)); haloGeo.translate(0, .5, 0);
  const glowGeo = keep(new THREE.PlaneGeometry(1, 1)); glowGeo.rotateX(-Math.PI / 2);
  let hkH = hk;   // 기둥 높이 배율 — 화면 크기로 하한을 둔다(size)
  const cells = fc.map(c => {
    const [x, z] = P(c.lon, c.lat);
    const core = new THREE.Mesh(coreGeo, beamMat()), halo = new THREE.Mesh(haloGeo, beamMat());
    const glow = new THREE.Mesh(glowGeo, keep(new THREE.MeshBasicMaterial({ map: glowTex, transparent: true, depthWrite: false,
      blending: light ? THREE.NormalBlending : THREE.AdditiveBlending, opacity: 0 })));
    for (const m of HALO ? [core, halo] : [core]) { m.position.set(x, 0, z); m.visible = false; scene.add(m); }
    glow.position.set(x, 6, z); glow.visible = false; scene.add(glow);
    return { key: c.key, x, z, core, halo, glow, state: null, h: 0, hT: 0, i: 0, iT: 0, col: new THREE.Color(), colT: new THREE.Color(), flash: 0 };
  });
  const cellOf = new Map(cells.map(c => [c.key, c]));

  // ── 열차 — 빛 덩어리 + 꼬리(뒤로 갈수록 작고 옅은 점) ──────────────────────────
  const rpts = ring.map(([lon, lat]) => P(lon, lat));
  const rlen = []; let acc = 0;
  for (let i = 0; i < rpts.length; i++) { rlen.push(acc); const a = rpts[i], b = rpts[(i + 1) % rpts.length]; acc += Math.hypot(b[0] - a[0], b[1] - a[1]); }
  const along = d => {   // 궤도 위 거리 → [x, z]
    d = ((d % acc) + acc) % acc; let lo = 0, hi = rlen.length - 1;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (rlen[mid] <= d) lo = mid; else hi = mid - 1; }
    const a = rpts[lo], b = rpts[(lo + 1) % rpts.length], seg = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1, t = (d - rlen[lo]) / seg;
    return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
  };
  const tailMats = Array.from({ length: TAIL_N + 1 }, (_, k) => keep(new THREE.SpriteMaterial({
    map: glowTex, color: light ? (k ? 0x4c9a1a : 0x2f6b0c) : (k ? 0xb6ff7a : 0xeaffd6), transparent: true, depthWrite: false,
    blending: light ? THREE.NormalBlending : THREE.AdditiveBlending,
    opacity: k ? .55 * Math.pow(1 - k / (TAIL_N + 1), 1.4) : 1 })));
  // 양방향 — 반은 궤도 순방향, 반은 역방향. 서로 엇갈린 자리에서 출발한다
  const trains = acc ? Array.from({ length: TRAINS }, (_, i) => ({ dir: i % 2 ? -1 : 1, off: LAP_MS * (Math.floor(i / 2) + (i % 2) * .5) / Math.ceil(TRAINS / 2), x: 0, z: 0,
    sp: tailMats.map((m, k) => { const s = new THREE.Sprite(m); const w = (k ? 260 * (1 - k / (TAIL_N + 2)) : 420) * (light ? .8 : 1); s.scale.set(w, w, 1); scene.add(s); return s; }) })) : [];

  // ── 화면 글자 — 3D 자리를 화면에 투영해 따라다닌다 ─────────────────────────────
  // tags: 노선 이름·첫 이상·이상 셀 이름표(우선 배치) / lbs: 지도와 같은 역·지명(등급·축척으로 거르고 겹치면 숨김)
  const tagsBox = $('.r3d-pins'), lbsBox = $('.r3d-lbs'), tags = [];
  const tag = (text, x, y, z, cls) => { const el = document.createElement('div'); el.className = 'r3d-tag' + (cls ? ' ' + cls : ''); el.textContent = text; tagsBox.appendChild(el); const t = { el, p: new THREE.Vector3(x, y, z), on: true, w: 0, h: 0, shown: null }; tags.push(t); return t; };
  if (rpts.length) { const n = rpts.reduce((a, p) => (p[1] < a[1] ? p : a), rpts[0]); tag(T(lineName || '야마노테선'), n[0], 0, n[1] - 250); }
  // 역·지명 — 지도 `LABELS` 그대로(`lb-s` 야마노테 · `lb-s2` 나머지 역 · `lb-a` 지명, `lb-o` 는 범위 밖 지명)
  const lbs = [];
  for (const l of opt.labels || []) {
    const [x, z] = S(l.x, l.y); if (Math.abs(x) > G || Math.abs(z) > G) continue;
    const isA = /\blb-a\b/.test(l.cls), isS1 = /\blb-s\b/.test(l.cls);
    const kind = isA ? 'a' : isS1 ? 's1' : 's2', tier = isA ? 9 : isS1 ? 1 : (l.tier || 3);
    // 주요 역 = 노선 수로 본 환승역(`tl` 2). 야마노테 역도 이것으로 가른다 — 늘 보이지 않는다(사용자 11-25). 옛 서버(`tl` 없음)는 야마노테를 주요로
    const major = (l.tl ?? (isS1 ? 2 : tier)) <= 2;
    lbs.push({ t: l.t, x, z, kind, tier, major, out: /\blb-o\b/.test(l.cls), el: null, w: 0, h: 0, shown: null, a: 0, px: 0, py: 0 });
  }
  // 겹칠 때 남기는 순서 — 1등급 역 → 2등급 → 지명 → 3등급
  const prio = l => l.kind === 's1' ? 0 : l.tier === 2 ? 1 : l.kind === 'a' ? 2 : 3;
  lbs.sort((a, b) => prio(a) - prio(b));
  for (const l of lbs) {
    const el = document.createElement('div'); el.className = `r3d-lb ${l.kind}${l.out ? ' o' : ''}`; el.textContent = l.t; el.style.display = 'none'; el.style.opacity = '0';
    lbsBox.appendChild(el); l.el = el;
  }
  // 가까운 역 — 셀 위치를 사람 말로(「目白駅付近」). 셀 이름(eNB-셀)은 일반 사람이 알아볼 수 없다
  const stns = lbs.filter(l => l.kind === 's1' || l.kind === 's2'), areas = lbs.filter(l => l.kind === 'a');
  const placeOf = (x, z) => {
    // 큰 역을 조금 앞세운다 — 바로 옆의 작은 역(上野御徒町)보다 사람들이 아는 역(御徒町·上野)으로 말하게
    let b = null, bs = Infinity;
    for (const s of stns) { const d = Math.hypot(s.x - x, s.z - z); if (d > NEAR_STN_M) continue; const sc = d + (s.kind === 's1' ? 0 : s.tier === 2 ? STN_PEN[0] : STN_PEN[1]); if (sc < bs) { bs = sc; b = s; } }
    if (b) return { name: b.t, stn: true };
    let a = null, ad = Infinity; for (const s of areas) { const d = Math.hypot(s.x - x, s.z - z); if (d < ad) { ad = d; a = s; } }
    return a ? { name: a.t, stn: false } : null;
  };
  const placeTxt = p => p ? T(p.stn ? '{s}역 부근' : '{s} 부근', { s: p.name }) : '';
  for (const c of cells) c.place = placeOf(c.x, c.z);
  // 여러 셀의 장소 — 가장 많이 걸린 역 이름 몇 개(「池袋〜大塚」)
  // 덩어리가 여럿이면 덩어리마다 가장 많은 장소를 먼저 하나씩(「주로 신주쿠 · 시부야」), 나머지는 셀 수 순
  const placesOf = (list, n) => {
    const m = new Map(), per = new Map();
    for (const c of list) if (c.place) {
      m.set(c.place.name, (m.get(c.place.name) || 0) + 1);
      const g = clOf.get(c.key); if (g == null) continue;
      const pm = per.get(g) || new Map(); pm.set(c.place.name, (pm.get(c.place.name) || 0) + 1); per.set(g, pm);
    }
    const out = [];
    for (const g of [...per.keys()].sort((a, b) => a - b)) { const best = [...per.get(g).entries()].sort((a, b) => b[1] - a[1])[0]; if (best && !out.includes(best[0])) out.push(best[0]); }
    for (const [k] of [...m.entries()].sort((a, b) => b[1] - a[1])) if (!out.includes(k)) out.push(k);
    return out.slice(0, n);
  };
  // ── 건물 — 두 겹(사용자 11-25 「지도만 보이니 허전하다」·「8m 까지, 보이는 구간만」) ──────────────────────────
  // ① 높은 건물(`building_tall`, 30m·8층 이상)과 역 띠 — 넓은 바닥 전체를 처음에 한 번 만든다
  // ② 낮은 건물(`/api/map/bldg`, 8m·3층 이상, JIS 3차 메시 칸) — 화면 가운데 축척이 `ST_T3_M`(1km) 이하일 때만, 가운데서
  //    축척 막대 값 × BLDG_VIEW_K 안의 칸을 가까운 칸부터 받아 칸마다 메시로 세운다. 범위를 벗어난 칸은 서서히 지우고 메모리를
  //    돌려준다. 높은 건물은 ①이 그리므로 칸에서는 뺀다(`t:1`). 칸 자료가 없으면(색인 없음) ①만 — 전과 같다
  // 실제 높이 그대로(`min_h` 부터). 깊이만 먼저 쓰고(건물끼리 앞뒤) 색을 칠한 뒤 깊이를 지운다 — 기둥·번짐·표식이 늘 위에.
  // 깊이 지우기는 건물 칠이 모두 끝난 뒤 한 번(renderOrder -1.4 의 빈 물체) — 칸이 여럿이라 칠할 때마다 지우면 서로를 못 가린다
  let bS = BLDG_K;
  const bHalf = G;
  const bldMeshes = [];
  const bOp = light ? .8 : .72, cB = new THREE.Color(light ? 0x9aa2b0 : 0x6b7f9f);
  const cS = new THREE.Color((getComputedStyle(document.body).getPropertyValue('--stbd') || '').trim() || (light ? '#b9bcc2' : '#5b6573'));
  const preMat = keep(new THREE.MeshBasicMaterial({ colorWrite: false, transparent: true, side: THREE.DoubleSide }));
  const paintMat = op => new THREE.MeshBasicMaterial({ vertexColors: true, transparent: true, opacity: op, depthWrite: false, depthFunc: THREE.LessEqualDepth, side: THREE.DoubleSide });
  {
    const clr = new THREE.Mesh(keep(new THREE.BufferGeometry().setAttribute('position', new THREE.Float32BufferAttribute([0, 0, 0, 0, 0, 0, 0, 0, 0], 3))),
      keep(new THREE.MeshBasicMaterial({ colorWrite: false, depthWrite: false, depthTest: false, transparent: true })));
    clr.frustumCulled = false; clr.renderOrder = -1.4; clr.onAfterRender = r => r.clearDepth(); scene.add(clr);
  }
  // 각기둥 — 벽(변마다 사각형)과 지붕(삼각 분할)만. 바닥면은 보이지 않으므로 만들지 않는다. 벽은 바깥 방향과 빛 방향으로 어둡기를 준다
  const Lx = .53, Lz = -.85, tcol = new THREE.Color();
  const area2 = a => { let s = 0; for (let i = 0, n = a.length; i < n; i++) { const p = a[i], q = a[(i + 1) % n]; s += p[0] * -q[1] - q[0] * -p[1]; } return s; };   // (x, −z) 평면 부호 면적×2
  const prism = (rings, h0, h1, base, pos, col) => {
    const put = (x, y, z, k) => { pos.push(x, y, z); tcol.copy(base).multiplyScalar(k); col.push(tcol.r, tcol.g, tcol.b); };
    const rs = rings.map((r, i) => (i === 0) === (area2(r) > 0) ? r : [...r].reverse());   // 바깥은 반시계, 구멍은 시계 — 벽의 바깥 방향이 맞게
    for (const r of rs) for (let i = 0, n = r.length; i < n; i++) {
      const [x1, z1] = r[i], [x2, z2] = r[(i + 1) % n], du = x2 - x1, dv = z1 - z2, L = Math.hypot(du, dv) || 1;
      const k = .6 + .32 * Math.max(0, (dv / L) * Lx + (du / L) * Lz);
      put(x1, h0, z1, k); put(x2, h0, z2, k); put(x2, h1, z2, k); put(x1, h0, z1, k); put(x2, h1, z2, k); put(x1, h1, z1, k);
    }
    const v2 = r => r.map(([x, z]) => new THREE.Vector2(x, -z)), all = rs.flat();
    for (const f of THREE.ShapeUtils.triangulateShape(v2(rs[0]), rs.slice(1).map(v2))) for (const j of f) put(all[j][0], h1, all[j][1], 1.22);
  };
  const meshOfArr = (pos, col, op) => {
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3)); g.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));
    const pre = new THREE.Mesh(g, preMat), paint = new THREE.Mesh(g, paintMat(op));
    pre.renderOrder = -1.6; paint.renderOrder = -1.5; pre.scale.y = paint.scale.y = bS; scene.add(pre, paint); bldMeshes.push(pre, paint);
    return { g, pre, paint };
  };
  const dropMesh = m => { scene.remove(m.pre, m.paint); m.g.dispose(); m.paint.material.dispose(); for (const x of [m.pre, m.paint]) { const i = bldMeshes.indexOf(x); if (i >= 0) bldMeshes.splice(i, 1); } };
  // 메인 스레드를 BLDG_SLICE_MS 이상 쥐지 않는다 — 넘으면 한 번 쉬고 잇는다(여는 전환·공전이 멈칫하지 않게)
  let tS = performance.now(), maxS = 0;
  const rest = async () => { const t = performance.now(); if (t - tS < BLDG_SLICE_MS) return true; maxS = Math.max(maxS, t - tS); await new Promise(r => setTimeout(r, 0)); tS = performance.now(); return alive; };
  const inBox = (x, z) => Math.abs(x) <= bHalf && Math.abs(z) <= bHalf;

  // ① 높은 건물·역 띠
  const buildTall = async (bl, sl) => {
    const tB = performance.now(); let nB = 0; tS = tB;
    const pos = [], col = [];
    const ringOf = r => { const a = r.map(([lon, lat]) => P(lon, lat)); if (a.length > 1 && a[0][0] === a[a.length - 1][0] && a[0][1] === a[a.length - 1][1]) a.pop(); return a; };
    const add = (rings, h0, h1, base) => {
      const rs = rings.map(ringOf).filter(r => r.length >= 3); if (!rs.length || h1 <= h0 || !inBox(...rs[0][0])) return;
      nB++; prism(rs, h0, h1, base, pos, col);
    };
    for (const b of bl) { if (b && b.rings && b.rings.length) add(b.rings, b.min_h || 0, b.h || 0, cB); if (!await rest()) return; }
    for (const b of sl) { if (b && b.kind === 'band' && b.ring) add([b.ring], 0, STN_H, cS); if (!await rest()) return; }
    maxS = Math.max(maxS, performance.now() - tS);
    wrap.dataset.bld = `${nB}/${pos.length / 3}/${Math.round(performance.now() - tB)}/${Math.round(maxS)}`;   // 잰 값(화면에 안 보임) — 수/꼭짓점/전체 ms/가장 길게 막은 ms
    if (pos.length) keep({ dispose: (m => () => dropMesh(m))(meshOfArr(pos, col, bOp)) });
    // 이름표 — 이름 있는 높은 건물만, 역·지명보다 뒤 순위(겹치면 숨김). 한 화면에 BLDG_LB_N 개까지
    const seen = new Set();
    for (const b of [...bl].sort((p, q) => (q.h || 0) - (p.h || 0))) {
      const t = (opt.lang === 'ja' ? b.ja || b.name : b.ko || b.name) || ''; if (!t || (b.h || 0) < BLDG_LB_H || seen.has(t)) continue;
      const r = b.rings[0]; let sx = 0, sz = 0; for (const [lon, lat] of r) { const [x, z] = P(lon, lat); sx += x; sz += z; }
      const x = sx / r.length, z = sz / r.length; if (!inBox(x, z)) continue;
      seen.add(t);
      const el = document.createElement('div'); el.className = 'r3d-lb b'; el.textContent = t; el.style.display = 'none'; lbsBox.appendChild(el);
      el.style.opacity = '0';
      lbs.push({ t, x, z, hy: b.h, kind: 'b', tier: 9, out: false, el, w: 0, h: 0, shown: null, a: 0, px: 0, py: 0 });
    }
  };
  const tallP = Promise.all([layerOf('building_tall'), opt.stations ? Promise.resolve(opt.stations) : layerOf('station')])
    .then(([bl, sl]) => alive ? buildTall(bl || [], sl || []) : null).catch(() => {});

  // ② 낮은 건물 칸
  const grid = { idx: null, cells: new Map(), q: [], busy: false, fetching: false, at: 0, bar: Infinity, ms: 0, n: 0 };
  bldgIndex().then(ix => { if (alive && ix) tallP.then(() => { grid.idx = ix; }); });
  const unP = (x, z) => [lon0 + x / KX, (360 / Math.PI) * Math.atan(Math.exp((my0 - z / KX) * Math.PI / 180)) - 90];
  const C_LAT = 30 / 3600, C_LON = 45 / 3600;
  const cellsAround = (x, z, r) => {
    const [lon, lat] = unP(x, z), dLat = r / 111320, dLon = r / (111320 * Math.cos(lat * RAD)), out = [];
    for (let iy = Math.floor((lat - dLat) / C_LAT); iy <= Math.floor((lat + dLat) / C_LAT); iy++)
      for (let ix = Math.floor((lon - 100 - dLon) / C_LON); ix <= Math.floor((lon - 100 + dLon) / C_LON); ix++) {
        const cl = (iy + .5) * C_LAT, co = 100 + (ix + .5) * C_LON, [cx, cz] = P(co, cl), d = Math.hypot(cx - x, cz - z);
        if (d <= r + 750) out.push({ code: meshCode(co, cl), d });   // 750 ≈ 칸 반대각선 — 원에 걸치는 칸까지
      }
    return out.sort((a, b) => a.d - b.d);
  };
  const buildCell = async (code, cj) => {
    const pos = [], col = [], [ox, oy] = cj.o, q = cj.q || 1e-5; let n = 0;
    for (const b of cj.b || []) {
      if (b.t) continue;   // 높은 건물은 ①에 있다
      const rs = (b.r || []).map(f => { const a = []; for (let i = 0; i + 1 < f.length; i += 2) a.push(P(ox + f[i] * q, oy + f[i + 1] * q)); return a; }).filter(r => r.length >= 3);
      if (rs.length && (b.h || 0) > (b.m || 0)) { prism(rs, b.m || 0, b.h, cB, pos, col); n++; }
      if (!await rest()) return null;
    }
    return { n, m: pos.length ? meshOfArr(pos, col, 0) : null, verts: pos.length / 3 };
  };
  const pump = async () => {
    if (grid.busy) return; grid.busy = true;
    while (alive && grid.q.length) {
      const [code, cj] = grid.q.shift(), c = grid.cells.get(code);
      if (!c || c.leave) { if (c) grid.cells.delete(code); continue; }
      tS = performance.now();
      const r = await buildCell(code, cj); if (!r) break;
      maxS = Math.max(maxS, performance.now() - tS);
      if (grid.cells.get(code) !== c) { if (r.m) dropMesh(r.m); continue; }
      Object.assign(c, r, { st: 'ready', a: 0 });
    }
    grid.busy = false;
  };
  const dropCell = (code, c) => { if (c.m) dropMesh(c.m); grid.cells.delete(code); };
  keep({ dispose: () => { for (const [k, c] of [...grid.cells]) dropCell(k, c); } });
  const gridTick = (now, dt) => {
    // 칸 나타남·사라짐 — 이름표와 같은 걸음
    let nr = 0, nb = 0, nv = 0;
    for (const [k, c] of [...grid.cells]) {
      if (c.st !== 'ready') continue;
      c.a = Math.max(0, Math.min(1, c.a + (c.leave ? -1 : 1) * dt / TAG_FADE_MS));
      if (c.m) { c.m.paint.material.opacity = bOp * c.a; c.m.pre.visible = c.a > .5; }
      if (c.leave && c.a <= 0) { dropCell(k, c); continue; }
      if (!c.leave) { nr++; nb += c.n; nv += c.verts; }
    }
    if (!grid.idx || now - grid.at < BLDG_TICK_MS) return; grid.at = now;
    wrap.dataset.grid = `${nr}/${nb}/${nv}/${grid.ms}/${Math.round(maxS)}`;   // 잰 값(화면에 안 보임) — 칸/동/꼭짓점/마지막 받기 ms/가장 길게 막은 ms
    const close = grid.bar <= (opt.stScale && opt.stScale.t3 || 1000);
    const r = grid.bar * BLDG_VIEW_K, want = close ? cellsAround(ctl.target.x, ctl.target.z, r).filter(c => grid.idx.cells[c.code]) : [];
    const ws = new Set(want.map(c => c.code));
    for (const [k, c] of grid.cells) if (!ws.has(k)) c.leave = true; else c.leave = false;
    if (grid.fetching) return;
    const need = want.filter(c => !grid.cells.has(c.code)).slice(0, BLDG_BATCH).map(c => c.code);
    if (!need.length) return;
    for (const k of need) grid.cells.set(k, { st: 'loading', a: 0, leave: false });
    grid.fetching = true; const t0 = performance.now();
    fetch(`/api/map/bldg?cells=${need.join(',')}`).then(x => x.json()).then(j => {
      grid.ms = Math.round(performance.now() - t0);
      for (const k of need) { const c = grid.cells.get(k); const cj = j && j.cells && j.cells[k]; if (!c) continue; if (cj) grid.q.push([k, cj]); else { c.st = 'ready'; c.n = 0; c.verts = 0; c.m = null; } }
      pump();
    }).catch(() => { for (const k of need) grid.cells.delete(k); }).finally(() => { grid.fetching = false; });
  };
  // 열차 운행표 — 야마노테 역(지도 1등급 역 이름)을 궤도에 투영한 거리마다 서고, 역 사이는 사인 곡선으로 가속·감속한다.
  // 한 바퀴 시간(LAP_MS)은 그대로 — 정차 시간을 뺀 나머지를 역 사이 거리에 비례해 나눈다. 역이 셋 미만이면 고른 속도
  const projD = (x, z) => {
    let bd = Infinity, bt = 0;
    for (let i = 0; i < rpts.length; i++) {
      const a = rpts[i], b = rpts[(i + 1) % rpts.length], dx = b[0] - a[0], dz = b[1] - a[1], L2 = dx * dx + dz * dz || 1;
      const t = Math.max(0, Math.min(1, ((x - a[0]) * dx + (z - a[1]) * dz) / L2)), d = Math.hypot(a[0] + dx * t - x, a[1] + dz * t - z);
      if (d < bd) { bd = d; bt = rlen[i] + t * Math.sqrt(L2); }
    }
    return [bt, bd];
  };
  const stD = [];
  if (acc) for (const l of lbs) if (l.kind === 's1') { const [d, off] = projD(l.x, l.z); if (off < 400) stD.push(d); }
  stD.sort((a, b) => a - b);
  const stops = stD.filter((d, i) => !i || d - stD[i - 1] > 150);
  const plan = ds => {
    const n = ds.length, runAll = Math.max(LAP_MS * .5, LAP_MS - n * DWELL_MS), v = acc / runAll;
    let t0 = 0; return ds.map((d0, j) => { const len = ((ds[(j + 1) % n] - d0) + acc) % acc || acc, run = len / v, s = { t0, d0, len, run }; t0 += DWELL_MS + run; return s; });
  };
  const fwd = stops.length >= 3 ? plan(stops) : null, rev = fwd ? plan(stops.map(d => acc - d).sort((a, b) => a - b)) : null;
  const runAt = (sc, tm) => {
    tm = ((tm % LAP_MS) + LAP_MS) % LAP_MS;
    let s = sc[0]; for (const x of sc) { if (x.t0 <= tm) s = x; else break; }
    const k = Math.max(0, Math.min(1, (tm - s.t0 - DWELL_MS) / s.run));
    return s.d0 + s.len * (.5 - .5 * Math.cos(Math.PI * k));
  };
  const trainD = (tr, now) => {
    const tm = now + tr.off;
    if (!fwd) return tr.dir * tm / LAP_MS * acc;
    return tr.dir > 0 ? runAt(fwd, tm) : acc - runAt(rev, tm);
  };
  // 범례·범위 안내 — 이 장면에 있는 것만
  if (!trains.length) $('.r3d-leg .tr').parentNode.remove();
  if (noCell) { const d = document.createElement('div'); d.className = 'r3d-note'; d.textContent = expired ? expMsg : T('장애를 주입했지만 이상으로 잡힌 셀이 없습니다 — 노선과 열차만 보여 줍니다'); wrap.appendChild(d); }
  if (outN) { const d = document.createElement('div'); d.className = 'n'; d.textContent = T('화면 밖 {n}셀 — 동떨어져 있어 범위에서 뺐습니다', { n: outN }); $('.r3d-leg').appendChild(d); }

  // ── 분마다 숫자 — 추이 그래프·자막이 같이 쓴다 ─────────────────────────────────
  const cnt = frames.map(f => { let s = 0, d = 0; for (const k in (f.cells || {})) { const v = f.cells[k]; if (!v || !v.state) continue; if (v.state === 'severe') s++; else d++; } return { s, d, n: s + d }; });
  const litAt = i => cells.filter(c => { const v = frames[i].cells && frames[i].cells[c.key]; return v && v.state; });
  const steps = tl.steps || [];
  const stepAt = k => steps.find(s => s.step === k);
  const fStart = stepAt('fault_start'), fEnd = stepAt('fault_end');
  const verdict = stepAt('verdict:L2_EVENT') || steps.find(s => String(s.step).startsWith('verdict:') && s.root_code);
  const causeNm = verdict ? (opt.cause ? opt.cause(verdict.root_code) : verdict.root_code) : '';
  const firstI = cnt.findIndex(c => c.n > 0);
  let peakI = -1; cnt.forEach((c, i) => { if (c.n > 0 && (peakI < 0 || c.n > cnt[peakI].n)) peakI = i; });
  const endMs = fEnd ? ms(fEnd.at) : NaN;
  // 판정 끝 — 마지막으로 판정이 있던 분의 다음 분(중간에 비는 분은 끝으로 보지 않는다)
  let lastI = -1; cnt.forEach((c, i) => { if (c.n > 0) lastI = i; });
  const recI = lastI >= 0 && lastI < frames.length - 1 ? lastI + 1 : -1;
  const fmin = i => ms(frames[i].t);
  // ── 시간 배분 — A 창 시작~첫 판정 전 · B 첫~마지막 판정 분 · C 그 뒤 원인 판정 분까지(먼저 났으면 C_TAIL_MIN 분). 그 뒤 빈 분은 잘라 낸다
  const vIdx = verdict ? frames.findIndex(f => ms(f.t) + 59999 >= ms(verdict.at)) : -1;
  const vI = verdict ? (vIdx < 0 ? frames.length - 1 : vIdx) : -1;
  // 셀 판정이 없으면 판정 구간 대신 주입 구간(in_fault)을 가운데 구간으로 쓴다
  const inF = frames.map((f, i) => f.in_fault ? i : -1).filter(i => i >= 0);
  const s0 = noCell ? (inF.length ? inF[0] : 0) : firstI, s1 = noCell ? (inF.length ? inF[inF.length - 1] : frames.length - 1) : lastI;
  const endI = Math.min(frames.length - 1, vI > s1 ? vI : s1 + C_TAIL_MIN);
  const NF = endI + 1;   // 재생·막대·그래프가 쓰는 분 수
  const nA = s0, nB = s1 - s0 + 1, nC = endI - s1;
  const clampTo = (v, [a, b]) => Math.min(b, Math.max(a, v));
  const stepA = nA ? clampTo(SEG_MS / nA, AC_STEP) : 0, stepB = clampTo(SEG_MS / nB, noCell ? [AC_STEP[0], B_STEP[1]] : B_STEP), stepC = nC ? clampTo(SEG_MS / nC, AC_STEP) : 0;
  // 셀이 없으면 가운데 구간에 읽을 이름표가 없다 — 0.5배 하한을 두지 않고 목표 길이에 맞춘다
  const stepOf = i => i < s0 ? stepA : i <= s1 ? stepB : stepC;
  // 노선 셀 합산 지표(맥스 `measured`) — 판정값이 아니라 퍼짐을 보는 참고 눈금. 빈 분(None)은 선을 끊는다. 집계가 없으면(rows 0) 그리지 않는다
  const MS = data.ta.measured && data.ta.measured.rows > 0 && data.ta.measured.points ? data.ta.measured : null;
  const mByT = new Map(MS ? MS.points.map(p => [p.t, p.value]) : []);
  const mv = MS ? frames.map(f => { const v = mByT.get(f.t); return v == null || !isFinite(v) ? null : +v; }) : null;
  const mThr = MS && MS.threshold ? MS.threshold : {};
  let mPk = -1; if (mv) for (let i = s0; i <= s1; i++) if (mv[i] != null && (mPk < 0 || mv[i] > mv[mPk])) mPk = i;
  wrap.dataset.plan = JSON.stringify({ A: [nA, Math.round(nA * stepA)], B: [nB, Math.round(nB * stepB)], C: [nC, Math.round(nC * stepC)],
    out: OUT_HOLD_MS + OUT_PULL_MS, cut: frames.length - NF });

  // ── 자막 — 장의 흐름. 문구는 timeline·시나리오 설명·원인 이름, 장소는 가까운 역에서 ─────────────
  const caps = [{ at: -Infinity, main: T('{name} — 평소 상태', { name: title || `F-${fid}` }), sub: '' }];
  // 「장애 시작」이라 하지 않는다 — 주입 시각은 탐지가 아니라 정답이다(실제 운영에서는 알 수 없다). 판정은 그 뒤에 따로 온다(사용자 11-25)
  if (fStart) caps.push({ at: ms(fStart.at), main: T('{t} 장애 주입 (정답 시각)', { t: hhmm(fStart.at) }),
    sub: [T('실제 운영에서는 알 수 없는 시각입니다'), opt.what || ''].filter(Boolean).join(' — ') });
  const mK = MS ? (opt.kpi ? opt.kpi(MS.kpi) : MS.kpi) || MS.kpi : '';
  const fmtP = v => (Math.round(v * 10) / 10).toString();
  if (noCell && mPk >= 0) {
    const a = { t: hhmm(frames[mPk].t), k: mK, v: fmtP(mv[mPk]), b: fmtP(mThr.major) };
    caps.push({ at: fmin(mPk), main: T('{t} 노선 셀 {k} 최고 {v}%', a),
      sub: mThr.major == null ? '' : mv[mPk] < mThr.major ? T('셀 판정 기준({b}%)에 닿지 않아 이상으로 잡히지 않았습니다', a) : T('셀 판정 기준({b}%)을 넘었지만 셀 단위 판정은 없었습니다', a) });
  }
  if (firstI >= 0) {
    const p = placesOf(litAt(firstI), 2).join('〜'), a = { t: hhmm(frames[firstI].t), n: cnt[firstI].n, p };
    // 주입 뒤 몇 분 — 주입 시각(timeline fault_start)과 첫 판정 분(frames)의 차
    if (fStart) a.m = Math.max(0, Math.round((fmin(firstI) - ms(fStart.at)) / 60000));
    const k = fStart ? (p ? '{t} {p} 부근에서 처음으로 이상 판정 — 주입 {m}분 뒤 (셀 {n}개)' : '{t} 처음으로 이상 판정 — 주입 {m}분 뒤 (셀 {n}개)')
      : (p ? '{t} {p} 부근에서 처음으로 이상이 잡혔습니다 (셀 {n}개)' : '{t} 처음으로 이상이 잡혔습니다 (셀 {n}개)');
    caps.push({ at: fmin(firstI), main: T(k, a), sub: '', fa: true });
  }
  if (peakI > firstI) {
    const p = placesOf(litAt(peakI), 3).join(' · ');
    caps.push({ at: fmin(peakI), main: T('{t} 가장 넓게 번졌습니다 — 심각 {s} · 나빠짐 {d}', { t: hhmm(frames[peakI].t), s: cnt[peakI].s, d: cnt[peakI].d }),
      sub: p ? T('주로 {p} 부근', { p }) : '' });
  }
  if (verdict) caps.push({ at: ms(verdict.at), main: T('원인: {c}', { c: causeNm }), sub: verdict.lead_min != null ? T('장애 시작 후 {m}분에 판정했습니다', { m: Math.round(verdict.lead_min) }) : '' });
  // 「회복」이라 하지 않는다 — 탐지가 5분 칸 경계에서 판정을 한꺼번에 끊어(F-607 16:49 41셀 → 16:50 0셀) 데이터는 그 뒤에도
  // 서서히 돌아온다(에스터 11-25 조사). 사실은 「판정이 더 이상 없다」까지다. 기둥은 천천히 내려앉힌다(FALL_MS)
  if (recI >= 0) caps.push({ at: fmin(recI), main: T('{t} 이상 판정이 더 이상 없습니다', { t: hhmm(frames[recI].t) }), sub: '' });
  else if (fEnd) caps.push({ at: endMs, main: T('{t} 장애 끝', { t: hhmm(fEnd.at) }), sub: '' });
  caps.sort((a, b) => a.at - b.at);
  // 끝마무리 요약 — 무엇이·언제·얼마나·어디서·원인. 전부 timeline·frames 값
  const sumCap = noCell ? (() => {
    const a = { t0: hhmm(frames[s0].t), t1: hhmm(frames[s1].t), m: nB };
    const main = `${title || `F-${fid}`} · ` + T(expired ? '{t0}〜{t1} 주입({m}분) · 판정 기록 보관 기간 지남' : '{t0}〜{t1} 주입({m}분) · 탐지되지 않은 주입', a);
    const sub = mPk >= 0 && mThr.major != null ? T('노선 셀 {k} 최고 {v}% · 셀 판정 기준 {b}%', { k: mK, v: fmtP(mv[mPk]), b: fmtP(mThr.major) }) : '';
    return { main, sub };
  })() : (() => {
    const p = placesOf(litAt(peakI), 3).join(' · ');
    const main = `${title || `F-${fid}`} · ` + T('{t0}〜{t1}({m}분) · 최대 {n}셀', { t0: hhmm(frames[firstI].t), t1: hhmm(frames[lastI].t), m: nB, n: cnt[peakI].n });
    const sub = [p ? T('주로 {p} 부근', { p }) : '', causeNm ? T('원인: {c}', { c: causeNm }) : ''].filter(Boolean).join(' · ');
    return { main, sub };
  })();
  let capI = -1, capWant = 0, capAt = -1e9, capOver = null, faTimer = 0;
  const faStep = stepAt('first_anomaly'), faCell = faStep && faStep.detail && cellOf.get(faStep.detail.cell);
  const faTag = faCell ? tag(T('첫 이상'), faCell.x, (H_DEG + 200) * hk, faCell.z, 'fa') : null;
  if (faTag) { faTag.on = false; faTag.el.onclick = e => { e.stopPropagation(); openTip(faCell); }; }
  // 자막 — 한 장은 최소 CAP_MIN_MS 둔다. 재생 중에는 밀린 장을 건너뛰지 않고 하나씩, 시간을 옮기면 바로 그 장으로
  const capRender = c => {
    const box = $('.r3d-cap');
    box.classList.add('fade');
    setTimeout(() => { if (!alive) return; box.querySelector('b').textContent = c.main; box.querySelector('small').textContent = c.sub; box.classList.remove('fade'); }, 450);
    // 시간을 옮겨 지난 장으로 돌아가도 다시 뜬다
    if (faTag) { clearTimeout(faTimer); faTag.on = !!c.fa; if (c.fa) faTimer = setTimeout(() => { faTag.on = false; }, 6000); }
  };
  const showCap = (upto, jump) => {
    let k = 0; caps.forEach((c, i) => { if (c.at <= upto) k = i; });
    capWant = k; if (jump) { capOver = null; capAt = -1e9; capI = -2; }
  };
  const syncCap = now => {
    if (now - capAt < CAP_MIN_MS) return;
    if (capOver) { if (capI !== 'over') { capI = 'over'; capAt = now; capRender(capOver); } return; }
    if (capWant === capI) return;
    capI = capI >= 0 && capI < capWant ? capI + 1 : capWant; capAt = now; capRender(caps[capI]);
  };

  // ── 이상 셀 이름표 — 그 분에 심각한 셀 중 값이 큰 것부터, 같은 장소는 하나만 ──────────────────
  const fmtV = v => v == null || !isFinite(v) ? '—' : Math.abs(v) >= 100 ? Math.round(v).toLocaleString() : (+v).toFixed(1);
  // `baseline` 은 평소 값이 아니라 그 등급의 판정 기준선이다(나빠짐 20 · 심각 40 같은). 자료에 없는 평소 값을 만들지 않는다
  const fmtB = v => v == null || !isFinite(v) ? '—' : Number.isInteger(+v) ? String(+v) : fmtV(v);
  const kpiNm = k => (opt.kpi ? opt.kpi(k) : k) || k;
  // ── 장애 종류 표식 — 이름표 자리(셀이 몰린 상위 장소)마다 기둥 위에 뜨는 3D 배지. 종류는 주입 kind 로 정한다.
  // 기본 도형과 돌출(ExtrudeGeometry)만 쓴다(외부 모델·CDN 없음). 노선 장애에는 띄우지 않는다(그 장면은 열차가 말한다)
  const kind = String((ta.fault && ta.fault.kind) || '');
  const mType = onRoute || noCell ? null
    : /^(ran_drop|mobility|ims_)/.test(kind) ? 'drop'
    : /^(transport_|apn_|gw_|dns|ipv6|cdn|external)/.test(kind) ? 'data'
    : /^ran_radio/.test(kind) ? 'radio'
    : /^(ran_congestion|event_crowd|capacity_shift)/.test(kind) ? 'crowd'
    : /^signaling_storm/.test(kind) ? 'storm' : 'nx';
  const mName = { drop: '통화 끊김', data: '데이터 안 됨', radio: '전파 약함', crowd: '혼잡', storm: '단말 동시 재접속', nx: '장애' }[mType] || '';
  const MS3 = MARK_K * R, mSize = Math.min(MARK_M[1], Math.max(MARK_M[0], MS3));
  const mkShape = pts => { const sh = new THREE.Shape(); pts.forEach(([x, y], i) => i ? sh.lineTo(x, y) : sh.moveTo(x, y)); sh.closePath(); return sh; };
  const ex = (shapes, depth) => keep(new THREE.ExtrudeGeometry(shapes, { depth, bevelEnabled: false, curveSegments: 12 }));
  const box = (w, h, x, y, rot) => mkShape([[-w / 2, -h / 2], [w / 2, -h / 2], [w / 2, h / 2], [-w / 2, h / 2]].map(([px, py]) => {
    const c = Math.cos(rot || 0), s2 = Math.sin(rot || 0); return [x + px * c - py * s2, y + px * s2 + py * c]; }));
  const circ = (r, x, y) => { const sh = new THREE.Shape(); sh.absarc(x, y, r, 0, Math.PI * 2, false); return sh; };
  // SVG 경로(M·L·H·V·C·Z, 대소문자) → Shape. 시나리오 카드의 그림을 그대로 옮기려고 쓴다(y 는 위로 뒤집는다)
  const pathShape = (d, tx) => {
    const sh = new THREE.Shape(), tok = d.match(/[a-zA-Z]|-?(?:\d*\.\d+|\d+)(?:e-?\d+)?/g) || [];
    let i = 0, cmd = '', x = 0, y = 0, sx = 0, sy = 0;
    const n = () => +tok[i++], P = (a, b) => tx(a, b);
    while (i < tok.length) {
      if (/[a-zA-Z]/.test(tok[i])) cmd = tok[i++];
      else if (/[zZ]/.test(cmd)) { i++; continue; }
      if (cmd === 'M') { x = n(); y = n(); sh.moveTo(...P(x, y)); sx = x; sy = y; cmd = 'L'; }
      else if (cmd === 'm') { x += n(); y += n(); sh.moveTo(...P(x, y)); sx = x; sy = y; cmd = 'l'; }
      else if (cmd === 'L') { x = n(); y = n(); sh.lineTo(...P(x, y)); }
      else if (cmd === 'l') { x += n(); y += n(); sh.lineTo(...P(x, y)); }
      else if (cmd === 'H') { x = n(); sh.lineTo(...P(x, y)); } else if (cmd === 'h') { x += n(); sh.lineTo(...P(x, y)); }
      else if (cmd === 'V') { y = n(); sh.lineTo(...P(x, y)); } else if (cmd === 'v') { y += n(); sh.lineTo(...P(x, y)); }
      else if (cmd === 'C') { const a = n(), b = n(), c = n(), e = n(); x = n(); y = n(); sh.bezierCurveTo(...P(a, b), ...P(c, e), ...P(x, y)); }
      else if (cmd === 'c') { const a = x + n(), b = y + n(), c = x + n(), e = y + n(); x += n(); y += n(); sh.bezierCurveTo(...P(a, b), ...P(c, e), ...P(x, y)); }
      else if (cmd === 'Z' || cmd === 'z') { sh.closePath(); x = sx; y = sy; }
      else i++;   // 모르는 명령은 건너뛴다
    }
    return sh;
  };
  // 시나리오 카드(`scenarios.html` 「이동 중 통화 끊김」)의 수화기 경로 — 24×24 아이콘 좌표, 가운데(12,12)를 원점으로
  const HANDSET = 'M6.62 10.79c1.44 2.83 3.76 5.14 6.59 6.59l2.2-2.2c.27-.27.67-.36 1.02-.24 1.12.37 2.33.57 3.57.57.55 0 1 .45 1 1V20c0 .55-.45 1-1 1-9.39 0-17-7.61-17-17 0-.55.45-1 1-1h3.5c.55 0 1 .45 1 1 0 1.25.2 2.45.57 3.57.11.35.03.74-.25 1.02l-2.2 2.2z';
  const slash = th => [box(2.1, th, 0, 0, Math.PI / 4)];   // 수화기가 ↘ 로 누워 있으므로 ↗ 로 긋는다(겹치지 않게)
  const xmark = (th, r, x, y) => [box(r, th, x, y, Math.PI / 4), box(r, th, x, y, -Math.PI / 4)];
  // 선(stroke)을 두께 있는 막대로 — 카드 그림은 선으로 그려져 있다
  const seg = (x1, y1, x2, y2, w) => box(Math.hypot(x2 - x1, y2 - y1) + w, w, (x1 + x2) / 2, (y1 + y2) / 2, Math.atan2(y2 - y1, x2 - x1));
  const rrect = (w, h, r, x, y) => { const sh = new THREE.Shape(), x0 = x - w / 2, y0 = y - h / 2;
    sh.moveTo(x0 + r, y0); sh.lineTo(x0 + w - r, y0); sh.quadraticCurveTo(x0 + w, y0, x0 + w, y0 + r); sh.lineTo(x0 + w, y0 + h - r);
    sh.quadraticCurveTo(x0 + w, y0 + h, x0 + w - r, y0 + h); sh.lineTo(x0 + r, y0 + h); sh.quadraticCurveTo(x0, y0 + h, x0, y0 + h - r);
    sh.lineTo(x0, y0 + r); sh.quadraticCurveTo(x0, y0, x0 + r, y0); return sh; };
  // 종류별 그림 — **시나리오 카드(`scenarios.html`)의 그림만** 쓴다. 바탕판 없이 그림 하나(body, 심각도 색) + 상태 표시 하나(mark, 흰색·밝은 배색이면 검정).
  // mk2 는 표시 뒤에 까는 body 색 테두리. 카드에 맞는 그림이 없는 종류는 카드의 ✕ 배지(`#nx`) 하나로
  const iconOf = t => {
    // 「이동 중 통화 끊김」 카드의 수화기(24×24) + 끊김 사선
    if (t === 'drop') return { body: [pathShape(HANDSET, (x, y) => [(x - 12) / 9, -(y - 12) / 9])], mark: slash(.2), mk2: slash(.42) };
    // 공용 `#bs` 기지국 — M-22 34L0-32l22 66 M-13 9h26 + 꼭대기 원(r4). 9번 무선 품질 저하 카드가 쓴다
    if (t === 'radio') { const k = 1 / 40, T2 = (x, y) => [x * k, -(y + 6) * k], L = (a, b, c, d) => seg(...T2(a, b), ...T2(c, d), 6 * k);
      return { body: [L(-22, 34, 0, -32), L(0, -32, 22, 34), L(-13, 9, 13, 9), circ(6 * k, ...T2(0, -43))] }; }
    // 공용 `#person` — 머리 원(cy −9, r7) + 몸(M-11 19V7a11 11 0 0 1 22 0v12Z). 1번 퇴근길 역 앞 카드가 여럿을 늘어놓는다
    if (t === 'crowd') { const man = (cx, cy, k) => { const P2 = (x, y) => [cx + x * k, cy - y * k];
        const b = new THREE.Shape(); b.moveTo(...P2(-11, 19)); b.lineTo(...P2(-11, 7)); b.absarc(cx, cy - 7 * k, 11 * k, Math.PI, 0, true); b.lineTo(...P2(11, 19)); b.closePath();
        return [circ(7 * k, ...P2(0, -9)), b]; };
      return { body: [...man(-.55, -.05, .03), ...man(.55, -.05, .03), ...man(0, .1, .038)] }; }
    // 11번 단말 동시 재접속 카드 — 같은 휴대폰(22×36, rx4)이 줄지어 한꺼번에 다시 붙는다
    if (t === 'storm') return { body: [-.62, 0, .62].flatMap((x, i) => [rrect(.46, .76, .09, x, i === 1 ? .12 : -.08)]) };
    // 그 밖(데이터 안 됨·장비 과부하 등) — 카드의 ✕ 배지 `#nx`(원 r18 + ✕ M-7-7l14 14m0-14-14 14, 두께 3)
    return { body: [circ(1, 0, 0)], mark: xmark(.26, 1.1, 0, 0) };
  };
  const icon = mType ? iconOf(mType) : null;
  // 두께를 줘서 돌 때 입체로 보이게 — 앞면은 심각도 색, 옆면은 어둡게(그룹 0 앞뒤면 · 1 옆면)
  const iGeo = icon ? Object.fromEntries(Object.entries(icon).map(([k, v]) => {
    const d = k === 'body' || k === 'faint' ? .32 : .08, g = ex(v, d); g.translate(0, 0, k === 'mark' ? .36 : k === 'mk2' ? .3 : -d / 2); return [k, g]; })) : {};
  const MARKC = light ? 0x111827 : 0xffffff;
  const markOf = () => {
    const g = new THREE.Group(), mats = [], body = [];
    const mat = (c, op) => { const m = keep(new THREE.MeshBasicMaterial({ color: c, transparent: true, opacity: 0, depthWrite: false })); m.userData.op = op; mats.push(m); return m; };
    const two = (geo, op) => { const f = mat(0xff4d4d, op), sd = mat(0x7f1d1d, op); body.push([f, sd]); return new THREE.Mesh(geo, [f, sd]); };
    if (iGeo.body) g.add(two(iGeo.body, 1));
    if (iGeo.faint) g.add(two(iGeo.faint, .3));
    if (iGeo.mk2) g.add(two(iGeo.mk2, 1));
    if (iGeo.mark) g.add(new THREE.Mesh(iGeo.mark, mat(MARKC, 1)));
    g.visible = false; g.renderOrder = 5; g.children.forEach((m, i) => { m.renderOrder = 5 + i; });
    scene.add(g);
    return { g, mats, body, a: 0 };
  };
  if (mType) { const d = document.createElement('div'); d.innerHTML = `<i class=mk></i>${esc(T('표식 = 장애 종류: {k}', { k: T(mName) }))}`; $('.r3d-leg').insertBefore(d, $('.r3d-leg').children[2] || null); }
  // 자리(slot) 4개 — 한 번 뜬 이름표는 TAG_MIN_MS 는 둔다. 같은 장소가 계속 상위면 다시 띄우지 않고 값만 바꾼다.
  // 바꿀 때는 최소 시간이 지난 것부터 TAG_SWAP_MS 마다 하나씩 — 천천히 사라지고(1초) 천천히 나타난다(0.8초)
  const keyOf = c => c.place ? c.place.name : c.key;
  const cellTags = Array.from({ length: CELL_TAGS }, () => { const t = tag('', 0, 0, 0, 'cl out'); t.on = false; t.cellTop = true; t.k = null; t.since = 0; t.leave = 0; t.inAt = 0; t.sev = true;
    t.a = 0; t.slot = -1; t.px = 0; t.py = 0; t.slideAt = 0; t.slideFrom = null; t.blockAt = 0; t.el.style.display = 'none';
    t.mk = mType ? markOf() : null;
    t.el.onclick = e => { e.stopPropagation(); if (t.cell) openTip(t.cell); }; return t; });
  const tagCls = t => { t.el.className = 'r3d-tag cl' + (t.sev ? '' : ' d') + (t.leave || t.inAt ? ' out' : ''); };
  const tagText = (t, r) => {
    const k = (r.v.kpis && r.v.kpis[0]) || r.v.kpi;
    t.cell = r.c; t.k = keyOf(r.c); t.sev = r.sev;
    t.el.innerHTML = `<b>${esc(placeTxt(r.c.place) || r.c.key)}</b> · ${esc(kpiNm(k))} ${fmtV(r.v.value)}`
      + (r.v.baseline != null ? ` <span style="opacity:.7">(${esc(T('판정 기준'))} ${fmtB(r.v.baseline)})</span>` : '');
    t.w = 0; tagCls(t);
  };
  let tagWant = [], lastSwap = -1e9;
  const fillCellTags = (i, jump) => {
    const f = frames[i], rows = [];
    for (const c of cells) { const v = f.cells && f.cells[c.key]; if (v && v.state) rows.push({ c, v, sev: v.state === 'severe' }); }
    rows.sort((a, b) => (b.sev - a.sev) || ((b.v.value ?? -Infinity) - (a.v.value ?? -Infinity)));
    const seen = new Set(), pick = [];
    // 덩어리가 여럿이면 덩어리마다 가장 나쁜 셀을 먼저 하나씩 — 한 곳 이름표만 4개 뜨지 않게
    const firstOf = new Map(); for (const r of rows) { const g = clOf.get(r.c.key); if (g != null && !firstOf.has(g)) firstOf.set(g, r); }
    if (firstOf.size > 1) for (const g of [...firstOf.keys()].sort((a, b) => a - b)) { const r = firstOf.get(g), k = keyOf(r.c); if (seen.has(k) || pick.length >= CELL_TAGS) continue; seen.add(k); pick.push(r); }
    for (const r of rows) { const k = keyOf(r.c); if (seen.has(k)) continue; seen.add(k); pick.push(r); if (pick.length >= CELL_TAGS) break; }
    tagWant = pick;
    const now = performance.now();
    if (jump) {   // 시간을 옮겼다 — 기다리지 않고 그 분의 이름표로
      cellTags.forEach((t, j) => { const r = pick[j]; t.leave = t.inAt = 0; t.on = !!r; if (r) { tagText(t, r); t.since = now - TAG_MIN_MS; } else { t.cell = null; t.k = null; }
        t.a = r ? 1 : 0; t.slot = -1; t.slideAt = 0; if (t.mk) t.mk.a = t.a; });
      return;
    }
    // 떠 있는 이름표의 값만 그 분 것으로(같은 장소가 상위면 그 셀로, 아니면 그 셀이 아직 켜져 있을 때만)
    const byK = new Map(pick.map(r => [keyOf(r.c), r]));
    for (const t of cellTags) {
      if (!t.k || t.leave) continue;
      const r = byK.get(t.k) || rows.find(x => x.c === t.cell);
      if (r) tagText(t, r);
    }
  };
  const syncTags = now => {
    for (const t of cellTags) {
      if (t.leave && now >= t.leave) { t.leave = 0; t.on = false; t.cell = null; t.k = null; tagCls(t); }
      if (t.inAt && now - t.inAt > 40) { t.inAt = 0; tagCls(t); }
    }
    const wantK = new Set(tagWant.map(r => keyOf(r.c)));
    const shownK = new Set(cellTags.filter(t => t.k && !t.leave).map(t => t.k));
    const waiting = tagWant.filter(r => !shownK.has(keyOf(r.c)));
    // 빈 자리에 새 이름표
    for (const r of waiting) {
      const t = cellTags.find(x => !x.k && !x.leave); if (!t) break;
      tagText(t, r); t.on = true; t.since = now; t.inAt = now; t.a = 0; t.slot = -1; t.slideAt = 0; if (t.mk) t.mk.a = 0; tagCls(t); shownK.add(t.k);
    }
    // 원하는 목록에 없는 것을 내린다 — 최소 시간이 지난 것부터 하나씩
    if (now - lastSwap < TAG_SWAP_MS) return;
    const old = cellTags.filter(t => t.k && !t.leave && !wantK.has(t.k) && now - t.since >= TAG_MIN_MS).sort((a, b) => a.since - b.since)[0];
    if (old) { old.leave = now + TAG_FADE_MS; tagCls(old); lastSwap = now; }
  };

  // ── 추이 그래프 — 분마다 이상 셀 수(심각·나빠짐 쌓기), 장애 구간 음영, 사건 눈금, 지금 세로선 ─
  const ch = $('.r3d-chart canvas');
  const CC = light ? { sev: '#dc2626', deg: '#e08a00', ax: '#d1d5db', tx: '#6b7280', vd: '#3f7a10', now: '#111827', sh: 'rgba(217,119,6,.12)', ml: '#0369a1' }
    : { sev: '#ff4d4d', deg: '#ffc23d', ax: '#3a4658', tx: '#7d8ea6', vd: '#8fdc4f', now: '#e6edf3', sh: 'rgba(255,184,107,.12)', ml: '#38bdf8' };
  if (mv) {
    $('.r3d-chart>div').innerHTML = noCell ? `${esc(mK)} — ${esc(T('노선 전체 평균(참고)'))} <span class=k>┄ ${esc(T('셀 판정 기준'))}</span>`
      : `${esc(T('이상 셀 수'))} <span class=k>· ${esc(T('선 = 노선 전체 평균(참고)'))} ┄ ${esc(T('셀 판정 기준'))}</span>`;
  }
  const drawChart = cur => {
    const dpr = Math.min(2, window.devicePixelRatio || 1), W = ch.clientWidth, H = ch.clientHeight;
    if (ch.width !== Math.round(W * dpr) || ch.height !== Math.round(H * dpr)) { ch.width = Math.round(W * dpr); ch.height = Math.round(H * dpr); }
    const c = ch.getContext('2d'); c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, W, H);
    const top = Math.max(1, ...cnt.slice(0, NF).map(x => x.n)), n = NF, bw = W / n, base = H - 14, hh = base - 6;
    const t0m = fmin(0), xAt = t => (t - t0m) / 60000 * bw;
    if (fStart) { c.fillStyle = CC.sh; const a = xAt(ms(fStart.at)), b = fEnd ? xAt(endMs) : W; c.fillRect(a, 0, b - a, base); }
    cnt.slice(0, NF).forEach((x, i) => {
      const hs = x.s / top * hh, hd = x.d / top * hh;
      c.globalAlpha = i <= cur ? 1 : .25; c.fillStyle = CC.sev; c.fillRect(i * bw + 1, base - hs, bw - 2, hs);
      c.fillStyle = CC.deg; c.fillRect(i * bw + 1, base - hs - hd, bw - 2, hd);
    });
    c.globalAlpha = 1; c.strokeStyle = CC.ax; c.beginPath(); c.moveTo(0, base + .5); c.lineTo(W, base + .5); c.stroke();
    c.fillStyle = CC.tx; c.font = '10px sans-serif'; c.textBaseline = 'top';
    c.fillText(hhmm(frames[0].t), 0, base + 3); const lt = hhmm(frames[n - 1].t); c.fillText(lt, W - c.measureText(lt).width, base + 3);
    if (!noCell) c.fillText(String(top), 2, 0);
    // 노선 합산 선과 셀 판정 기준(나빠짐·심각) 점선 — 셀이 있으면 막대 위에 옅게
    if (mv) {
      const vals = mv.slice(0, NF).filter(v => v != null);
      const mTop = Math.max(1, (mThr.critical || 0), ...vals) * 1.12, yv = v => base - v / mTop * hh;
      c.setLineDash([4, 3]); c.lineWidth = 1;
      for (const [v, col] of [[mThr.major, CC.deg], [mThr.critical, CC.sev]]) if (v != null) {
        c.strokeStyle = col; c.globalAlpha = noCell ? .9 : .5; c.beginPath(); c.moveTo(0, yv(v) + .5); c.lineTo(W, yv(v) + .5); c.stroke();
        c.globalAlpha = noCell ? 1 : .7; c.fillStyle = col; const s = `${fmtP(v)}%`; c.fillText(s, W - c.measureText(s).width - 2, yv(v) - 11);
      }
      c.setLineDash([]); c.strokeStyle = CC.ml; c.lineWidth = noCell ? 2 : 1.5; c.globalAlpha = noCell ? 1 : .6; c.beginPath();
      let pen = false;
      for (let i = 0; i < NF; i++) { const v = mv[i]; if (v == null) { pen = false; continue; } const x = (i + .5) * bw, y = yv(v); if (pen) c.lineTo(x, y); else c.moveTo(x, y); pen = true; }
      c.stroke(); c.globalAlpha = 1; c.lineWidth = 1;
      if (noCell && mPk >= 0) { c.fillStyle = CC.ml; const s = `${fmtP(mv[mPk])}%`; c.fillText(s, Math.min(W - 30, (mPk + .5) * bw + 3), Math.max(0, yv(mv[mPk]) - 12)); }
    }
    if (verdict) { const x = xAt(ms(verdict.at)); c.strokeStyle = CC.vd; c.setLineDash([3, 3]); c.beginPath(); c.moveTo(x, 0); c.lineTo(x, base); c.stroke(); c.setLineDash([]); }
    if (cur >= 0) { const x = (cur + .5) * bw; c.strokeStyle = CC.now; c.beginPath(); c.moveTo(x, 0); c.lineTo(x, base); c.stroke(); }
  };

  // ── 재생 — 지금 분(fi)·그 분에 머문 시간(tick)·속도. 막대·그래프·키로 분을 바로 옮긴다 ─────────
  const last = endI;   // 잘라 낸 뒤의 마지막 분
  let playing = true, fi = -1, tick = 0, speed = 1, ended = false, outro = null, zoomK = 1;
  const playBtn = $('.r3d-play');
  const setPlay = on => { playing = on; playBtn.textContent = on ? '❚❚' : ended ? '↺' : '▶'; };
  // instant — 끌어서 옮길 때는 기둥을 보간 없이 그 분 모양으로 바로 둔다
  const applyFrame = (i, instant) => {
    const f = frames[i];
    for (const c of cells) {
      const v = f.cells && f.cells[c.key], s = v ? v.state : null;
      c.state = s;
      c.hT = (s === 'severe' ? H_SEV : s ? H_DEG : 0) * hkH;
      c.iT = s === 'severe' ? 1 : s ? .8 : 0;
      if (s) c.colT.copy(s === 'severe' ? SEV : DEG);
      if (s && (!c.h || instant)) c.col.copy(c.colT);
      if (instant) { c.h = c.hT; c.i = c.iT; }
    }
    $('.r3d-t').textContent = hhmm(f.t) + (f.in_fault ? ` · ${T('장애 중')}` : '');
    $('.r3d-cnt').innerHTML = `<b class=s>● ${T('심각')} ${cnt[i].s}</b> <b class=d>● ${T('나빠짐')} ${cnt[i].d}</b>`;
    $('.r3d-bar i').style.width = `${(i + 1) / NF * 100}%`;
    showCap(ms(f.t) + 59999, instant);
    drawChart(i);
    fillCellTags(i, instant);
    if (tip) fillTip();
  };
  const goTo = (i, instant) => {
    i = Math.max(0, Math.min(last, i));
    if (ended && i < last) { ended = false; if (!playing) setPlay(false); }
    outro = null;
    fi = i; tick = 0; applyFrame(i, instant);
  };
  // 끝마무리 — 가장 넓던 분을 다시 띄우고, 요약 자막을 둔 채 천천히 멀어진 뒤 ↺ 로 멈춘다(자동으로 닫지 않는다)
  const startOutro = () => { goTo(noCell ? (mPk >= 0 ? mPk : s1) : peakI); outro = { t: 0 }; capOver = sumCap; };
  showCap(-Infinity); drawChart(-1);
  // 진행 막대에 세 구간을 옅게 — A 회색 · B 빨강 · C 초록
  { const pa = nA / NF * 100, pb = (nA + nB) / NF * 100, op = light ? .22 : .28;
    $('.r3d-bar').style.backgroundImage = `linear-gradient(90deg,rgba(128,128,128,${op * .5}) 0 ${pa}%,rgba(239,68,68,${op}) ${pa}% ${pb}%,rgba(34,197,94,${op}) ${pb}% 100%)`;
    $('.r3d-bar').title += ` — ${T('회색 판정 전 · 빨강 판정 중 · 초록 판정 끝')}`; }

  // ── 카메라 — 단계: top(지도와 같은 시점) → tilt(기울며 범위로) → run(자동 공전 / 손) → back(닫기 · 위로 돌아옴) ─────
  const center = new THREE.Vector3(0, 0, 0);
  let dist = R * 2.6;
  const fov = 40;
  const fit = () => {
    const vf = fov * RAD, hf = 2 * Math.atan(Math.tan(vf / 2) * cam.aspect);
    dist = R * .95 / Math.sin(Math.min(vf, hf) / 2) * (onRoute ? 1 : DIST_CELL_K);
  };
  // 위에서 본 지도 보기 — 세로로 지도의 세로 길이가 꼭 들어오는 높이(시야 창 밀기 없이)
  const dTop = mapH / 2 / Math.tan(fov / 2 * RAD);
  const topC = new THREE.Vector3(mapC[0], 0, mapC[1]);
  const pose = (c, d, el, az, out) => out.set(c.x + Math.sin(az) * Math.cos(el * RAD) * d, Math.sin(el * RAD) * d, c.z + Math.cos(az) * Math.cos(el * RAD) * d);
  let az = 0, lastNow = performance.now(), auto = true, back = null, phase = 'top', ph0 = 0, offK = 0, from = null;
  const ctl = new OrbitControls(cam, renderer.domElement);
  ctl.enableDamping = true; ctl.dampingFactor = .08;
  ctl.maxPolarAngle = 84 * RAD;            // 지면 아래로 못 내려가게
  ctl.screenSpacePanning = false;          // 이동은 지면을 따라
  ctl.enabled = false;                     // 전환이 끝나야 손으로 움직인다
  // 아래 자막·그래프 자리만큼 장면을 위로 올린다(시야 창을 아래로 밀기). 지도와 맞춘 시점에서는 밀지 않는다(offK 0)
  const setOff = () => { const w = wrap.clientWidth, h = wrap.clientHeight; cam.setViewOffset(w, h, 0, h * .06 * offK, w, h); cam.updateProjectionMatrix(); };
  const size = () => { const w = wrap.clientWidth, h = wrap.clientHeight; renderer.setSize(w, h, false); cam.aspect = w / Math.max(1, h);
    fit(); setOff(); ctl.minDistance = R * .12; ctl.maxDistance = Math.max(dist * 2.2, dTop * 1.2);
    // 기둥 하한 — 공전 시점에서 화면 높이의 PILLAR_MIN_SCR(나빠짐 기둥)·심 굵기 CORE_MIN_SCR 아래로 작아지지 않게
    const scrH = 2 * dist * Math.tan(fov / 2 * RAD);
    hkH = Math.max(hk, PILLAR_MIN_SCR * scrH / H_DEG);
    bS = BLDG_PILLAR ? Math.min(BLDG_K, BLDG_PILLAR * H_DEG * hkH / BLDG_REF_H) : BLDG_K; for (const m of bldMeshes) m.scale.y = bS;
    wrap.dataset.pillar = `${Math.round(H_DEG * hkH)}/${Math.round(H_SEV * hkH)}/${bS.toFixed(2)}`;   // 나빠짐/심각 기둥 높이(m)·건물 배율 — 비교용
    const rk = Math.max(1, CORE_MIN_SCR * scrH / (CORE_R * hk));
    for (const c of cells) { c.core.scale.x = c.core.scale.z = rk; c.halo.scale.x = c.halo.scale.z = rk; }
  };
  size(); const ro = new ResizeObserver(size); ro.observe(wrap);
  ctl.addEventListener('start', () => { auto = false; back = null; onTouch(); });
  // 끝난 뒤 조작 안내 — 십자 화살표 + 문구. 은은하게 깜빡이고, 끌거나 휠을 쓰면 서서히 사라진다
  const hint = document.createElement('div'); hint.className = 'r3d-hint';
  hint.innerHTML = `<div><svg width=40 height=40 viewBox="-27 -27 54 54" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">`
    + `<path d="M0-22V22M-22 0H22M-6-16L0-22 6-16M-6 16L0 22 6 16M-16-6L-22 0-16 6M16-6L22 0 16 6"/></svg>`
    + `<span>${esc(T('드래그해서 둘러보세요 · 휠로 확대'))}</span></div>`;
  wrap.appendChild(hint);
  const showHint = on => hint.classList.toggle('on', on);
  onTouch = () => showHint(false);
  const goHome = () => {
    zoomK = 1;
    // 지금 자리에서 가장 가까운 공전 각으로 돌아간다 — 반 바퀴 휙 돌지 않게
    az = Math.atan2(cam.position.x - ctl.target.x, cam.position.z - ctl.target.z);
    back = { p0: cam.position.clone(), q0: ctl.target.clone(), t: performance.now() };
  };

  // ── 셀 눌러 보기 — 기둥(심·후광)을 맞힌다. 값은 그 분 timelapse 프레임 그대로 ─────────────
  const ray = new THREE.Raycaster(), ndc = new THREE.Vector2();
  const meshCell = new Map(); for (const c of cells) { meshCell.set(c.core, c); meshCell.set(c.halo, c); }
  let tip = null;   // {el, cell}
  const nameOf = new Map((opt.cells || []).map(c => [c.key, c.name]));
  const fillTip = () => {
    const c = tip.cell, f = frames[Math.max(0, fi)], v = f.cells && f.cells[c.key];
    const st = v && v.state, nm = nameOf.get(c.key);
    let h = `<span class=x>✕</span><b>${esc(placeTxt(c.place) || `${T('셀')} ${c.key}`)}</b>`
      + `<div class=n>${esc(T('셀'))} ${esc(c.key)}${nm && nm !== `eNB${c.key}` ? ' · ' + esc(nm) : ''}</div><table>`
      + `<tr><td>${esc(T('시각'))}</td><td>${esc(hhmm(f.t))}</td></tr>`;
    let desc = '';
    if (!st) h += `<tr><td>${esc(T('등급'))}</td><td class=n>${esc(T('이 분 판정 없음'))}</td></tr>`;
    else {
      const ks = (v.kpis && v.kpis.length ? v.kpis : [v.kpi]).filter(Boolean);
      desc = ks.map(k => opt.kpiLong ? opt.kpiLong(k) : '').filter(Boolean)[0] || '';
      h += `<tr><td>${esc(T('등급'))}</td><td class=${st === 'severe' ? 's' : 'd'}>${esc(T(st === 'severe' ? '심각' : '나빠짐'))}</td></tr>`
        + `<tr><td>${esc(T('지표'))}</td><td>${ks.map(k => `${esc(kpiNm(k))}${kpiNm(k) !== k ? ` <span class=n>(${esc(k)})</span>` : ''}`).join(', ')}</td></tr>`
        + `<tr><td>${esc(T('값'))}</td><td>${fmtV(v.value)}${v.baseline != null ? ` <span class=n>· ${esc(T('판정 기준'))} ${fmtB(v.baseline)}(${esc(T(st === 'severe' ? '심각' : '나빠짐'))})</span>` : ''}</td></tr>`
        + (v.denom != null ? `<tr><td>${esc(T('표본'))}</td><td>${fmtV(v.denom)}</td></tr>` : '');
    }
    if (mType) h += `<tr><td>${esc(T('표식'))}</td><td>${esc(T(mName))}</td></tr>`;
    if (causeNm) h += `<tr><td>${esc(T('이 장애 원인'))}</td><td>${esc(causeNm)}</td></tr>`;
    tip.el.innerHTML = h + '</table>' + (desc ? `<p>${esc(desc)}</p>` : '');
    tip.el.querySelector('.x').onclick = e => { e.stopPropagation(); closeTip(); };
  };
  const openTip = c => { closeTip(); const el = document.createElement('div'); el.className = 'r3d-tip'; wrap.appendChild(el); tip = { el, cell: c }; fillTip(); };
  const closeTip = () => { if (tip) { tip.el.remove(); tip = null; } };
  const gl = renderer.domElement;
  let downAt = null;
  const onDown = e => { downAt = [e.clientX, e.clientY]; };
  const onUp = e => {
    if (!downAt || e.button !== 0) { downAt = null; return; }
    const moved = Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]); downAt = null;
    if (moved > CLICK_PX) return;   // 끌기였다
    const r = gl.getBoundingClientRect();
    ndc.set((e.clientX - r.left) / r.width * 2 - 1, -(e.clientY - r.top) / r.height * 2 + 1);
    ray.setFromCamera(ndc, cam);
    const mkHit = cellTags.filter(t => t.mk && t.mk.g.visible && t.cell).flatMap(t => t.mk.g.children.map(m => [m, t.cell]));
    const mkMap = new Map(mkHit);
    const hit = ray.intersectObjects([...mkHit.map(x => x[0]), ...cells.flatMap(c => c.core.visible ? (HALO ? [c.core, c.halo] : [c.core]) : [])], false)[0];
    if (hit) openTip(mkMap.get(hit.object) || meshCell.get(hit.object)); else closeTip();
  };
  gl.addEventListener('pointerdown', onDown); gl.addEventListener('pointerup', onUp);

  // ── 시간 이동 — 진행 막대(누르기·끌기)·추이 그래프(누르기) ─────────────────────────
  const bar = $('.r3d-bar');
  const barAt = e => { const r = bar.getBoundingClientRect(); return Math.floor((e.clientX - r.left) / r.width * NF); };
  let barDrag = false;
  const onBarMove = e => { if (barDrag) goTo(barAt(e), true); };
  const onBarUp = () => { barDrag = false; };
  bar.addEventListener('pointerdown', e => { e.preventDefault(); barDrag = true; goTo(barAt(e), true); });
  window.addEventListener('pointermove', onBarMove); window.addEventListener('pointerup', onBarUp);
  ch.addEventListener('pointerdown', e => { const r = ch.getBoundingClientRect(); goTo(Math.floor((e.clientX - r.left) / r.width * NF), true); });

  // ── 버튼·키 ─────────────────────────────────────────────────────────────
  const togglePlay = () => {
    if (ended) { showHint(false); goTo(0, true); setPlay(true); return; }   // ↺ 처음부터 다시
    setPlay(!playing);
  };
  playBtn.onclick = togglePlay;
  $('.r3d-home').onclick = () => goTo(0, true);
  $('.r3d-view').onclick = () => { if (phase === 'run') goHome(); };
  for (const b of wrap.querySelectorAll('.r3d-sp button')) b.onclick = () => {
    speed = +b.dataset.v; for (const x of wrap.querySelectorAll('.r3d-sp button')) x.classList.toggle('on', x === b);
  };
  keys = e => {
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { goTo((fi < 0 ? 0 : fi) + (e.key === 'ArrowLeft' ? -1 : 1), true); return true; }
    if (e.key === ' ' || e.code === 'Space') { togglePlay(); return true; }
    return false;
  };

  // ── 글자 배치 — 투영·축척 거르기·겹침 숨김. 이름표(tags)를 먼저 놓고 역·지명은 빈 자리에만 ──────────
  const v3 = new THREE.Vector3(), tc = new THREE.Color(), pa = new THREE.Vector3(), pb = new THREE.Vector3();
  const tanH = Math.tan(fov / 2 * RAD);
  let lastPlace = 0;
  const vis = BEAM_VIS[light ? 1 : 0];
  const place = (W, H) => {
    const pnow = performance.now(), pdt = lastPlace ? Math.min(100, pnow - lastPlace) : 16; lastPlace = pnow;
    const boxes = [];
    const free = (x0, y0, x1, y1) => !boxes.some(b => x0 < b[2] && x1 > b[0] && y0 < b[3] && y1 > b[1]);
    const scr = p => { v3.copy(p).project(cam); return v3.z < 1 && v3.z > -1 ? [(v3.x + 1) / 2 * W, (1 - v3.y) / 2 * H] : null; };
    const show = (o, on, x, y, anchorBottom) => {
      if (!on) { if (o.shown !== false) { o.el.style.display = 'none'; o.shown = false; } return false; }
      if (o.shown !== true) { o.el.style.display = ''; o.shown = true; }
      if (!o.w) { o.w = o.el.offsetWidth; o.h = o.el.offsetHeight; }
      const x0 = x - o.w / 2, y0 = anchorBottom ? y - o.h - 4 : y - o.h / 2;
      if (!free(x0 - 3, y0 - 2, x0 + o.w + 3, y0 + o.h + 2)) { o.el.style.display = 'none'; o.shown = false; return false; }
      boxes.push([x0 - 3, y0 - 2, x0 + o.w + 3, y0 + o.h + 2]);
      o.el.style.transform = `translate(${x0}px,${y0}px)`; return true;
    };
    // 화면 위 막대·아래 자막·그래프·범례 자리는 비워 둔다
    boxes.push([0, 0, W, noCell ? 104 : 64], [0, H - 140, 330, H], [W / 2 - 330, H - 100, W / 2 + 330, H], [W - 300, H - 120, W, H]);
    // 「첫 이상」은 이름표 배치의 1순위(사용자 11-25 「셀 카드에 자꾸 가려진다」). 그 셀의 카드가 떠 있으면 카드를 먼저 놓고
    // 바로 위에 붙인다(카드와 함께 움직인다). 다른 카드는 둘이 차지한 자리를 비켜 선다. 맨 위 겹(z-index)은 CSS
    // 카드는 장소(keyOf)로 묶이므로 그 셀이 아니라 같은 장소의 다른 셀 카드일 수 있다 — 장소로 찾는다(F-619 渋谷)
    const faCard = faTag && faTag.on ? cellTags.find(t => (t.cell === faCell || t.k === keyOf(faCell)) && t.a > .05 && t.slot >= 0) : null;
    const ord = faTag ? [...(faCard ? [faCard] : []), faTag, ...tags.filter(t => t !== faTag && t !== faCard)] : tags;
    for (const t of ord) {
      if (t === faTag && faCard) {
        if (t.shown !== true) { t.el.style.display = ''; t.shown = true; }
        if (!t.w) { t.w = t.el.offsetWidth; t.h = t.el.offsetHeight; }
        const ea = faCard.a * faCard.a * (3 - 2 * faCard.a), x0 = faCard.px + faCard.w / 2 - t.w / 2, y0 = faCard.py + (1 - ea) * TAG_RISE_PX - t.h - 3;
        boxes.push([x0 - 3, y0 - 2, x0 + t.w + 3, y0 + t.h + 2]);
        t.el.style.transform = `translate(${x0}px,${y0}px)`; continue;
      }
      // 카드가 없을 때도 겹침 검사로 숨기지 않는다 — 같은 기둥의 표식 자리(위에서 먼저 비워 둔 칸)에 늘 걸려 숨겨졌다(사용자 11-25 「여전히 가려진다」)
      if (t === faTag) {
        t.p.y = Math.max(faCell.h * vis, H_DEG * hkH * vis) + 200 * hk;   // 기둥 꼭대기를 따라간다(기둥 높이 하한 hkH 가 붙은 뒤로 고정 높이는 기둥 속에 묻혔다)
        const s = t.on && scr(t.p);
        if (!s) { if (t.shown !== false) { t.el.style.display = 'none'; t.shown = false; } continue; }
        if (t.shown !== true) { t.el.style.display = ''; t.shown = true; }
        if (!t.w) { t.w = t.el.offsetWidth; t.h = t.el.offsetHeight; }
        const x0 = s[0] - t.w / 2, y0 = s[1] - t.h - 4;
        boxes.push([x0 - 3, y0 - 2, x0 + t.w + 3, y0 + t.h + 2]);
        t.el.style.transform = `translate(${x0}px,${y0}px)`; continue;
      }
      // 이상 셀 이름표 — 기둥 꼭대기가 화면 위 막대에 걸리거나 다른 글자와 겹치면 기둥 중간·밑동에 단다(깜박 사라지지 않게)
      // 즉시 감추거나 다른 자리로 튀지 않는다 — 한 번 잡은 자리(slot)를 지키고, 막히면 TAG_BLOCK_MS 버틴 뒤 다른 자리로 미끄러지며,
      // 놓을 데가 없으면 그 자리에서 내려가며 사라진다. 나타날 때는 아래에서 올라온다(사용자 11-25)
      if (t.cellTop) {
        const c = t.cell, want = !!(c && t.on && t.k && !t.leave);
        let q = null, slot = -1;
        if (c && t.on) {
          if (!t.w) { t.el.style.display = ''; t.w = t.el.offsetWidth; t.h = t.el.offsetHeight; }
          // 첫 자리는 기둥 꼭대기 — 표식이 있으면 그 지름만큼 화면에서 위로 띄워 둘 사이에 표식 자리를 남긴다(3D 높이로 띄우면 낮은 각도에서 어긋났다)
          const top = Math.max(c.h * vis, 60 * hk), mk = t.mk && t.mk.g.visible;
          const mr = mk ? (pb.set(c.x, top, c.z), mSize * (t.sev ? 1 : .8) * 1.05 * H / (2 * cam.position.distanceTo(pb) * tanH)) : 0;
          const ys = [top, top, top * .5, 0], offs = [mk ? -(2 * mr + 6) : 0, 0, 0, 0];
          const at = j => { t.p.set(c.x, ys[j], c.z); const s = scr(t.p); if (!s) return null; const x0 = s[0] - t.w / 2, y0 = s[1] + offs[j] - t.h - 4; return { x0, y0, ok: free(x0 - 3, y0 - 2, x0 + t.w + 3, y0 + t.h + 2) }; };
          const cur = t.slot >= 0 ? at(t.slot) : null;
          if (cur && cur.ok) { q = cur; slot = t.slot; t.blockAt = 0; }
          else {
            if (cur) { t.blockAt = t.blockAt || pnow; if (pnow - t.blockAt < TAG_BLOCK_MS) { q = cur; slot = t.slot; } }
            if (!q) for (let j = 0; j < ys.length; j++) { if (j === t.slot) continue; const r = at(j); if (r && r.ok) { q = r; slot = j; t.blockAt = 0; break; } }
          }
        }
        t.a = Math.max(0, Math.min(1, t.a + (want && q ? 1 : -1) * pdt / TAG_FADE_MS));
        if (q) {
          if (t.slot >= 0 && slot !== t.slot && t.a > .05) { t.slideFrom = [t.px, t.py]; t.slideAt = pnow; }   // 자리 옮김 — 미끄러진다
          if (t.slot < 0 || t.a <= .05) t.slideAt = 0;
          t.slot = slot;
          const k = t.slideAt ? Math.min(1, (pnow - t.slideAt) / TAG_SLIDE_MS) : 1, e = k * k * (3 - 2 * k);
          t.px = k < 1 ? t.slideFrom[0] + (q.x0 - t.slideFrom[0]) * e : q.x0; t.py = k < 1 ? t.slideFrom[1] + (q.y0 - t.slideFrom[1]) * e : q.y0;
          if (k >= 1) t.slideAt = 0;
        }
        if (t.a <= .005) {
          if (t.shown !== false) { t.el.style.display = 'none'; t.shown = false; }
          if (!want) t.slot = -1;   // 다음에 뜰 때는 가장 좋은 자리부터
          continue;
        }
        if (t.shown !== true) { t.el.style.display = ''; t.shown = true; }
        const ea = t.a * t.a * (3 - 2 * t.a);
        boxes.push([t.px - 3, t.py - 2, t.px + t.w + 3, t.py + t.h + 2]);
        t.el.style.opacity = ea.toFixed(3);
        t.el.style.transform = `translate(${t.px}px,${t.py + (1 - ea) * TAG_RISE_PX}px)`;
        continue;
      }
      const s = t.on && scr(t.p);
      show(t, !!s, s ? s[0] : 0, s ? s[1] : 0, true);
    }
    // 표식 — 카드 다음 순서로 **화면에서** 놓는다(사용자 11-25 「각도가 낮아지면 표식이 카드 위로 뜬다」 · 「공간이 없으면 옆으로」).
    // 기둥 꼭대기 바로 위 → 왼쪽 → 오른쪽 중 빈 자리(모두 꼭대기에서 표식 지름 1~2개 안). 자리 지키기·미끄러짐은 카드와 같다.
    // 정한 화면 자리를 기둥 꼭대기와 같은 깊이로 되돌려 3D 표식을 둔다 — 크기는 그대로, 기둥에서 멀어지지 않는다
    for (const t of cellTags) {
      const m = t.mk; if (!m || !m.g.visible || !t.cell) { if (m) m.slot = -1; continue; }
      const c = t.cell; pb.set(c.x, Math.max(c.h * vis, 60 * hk), c.z);
      v3.copy(pb).project(cam); if (v3.z <= -1 || v3.z >= 1) continue;
      const zN = v3.z, sx = (v3.x + 1) / 2 * W, sy = (1 - v3.y) / 2 * H;
      const mr = mSize * (t.sev ? 1 : .8) * 1.05 * H / (2 * cam.position.distanceTo(pb) * tanH), g = 4;
      const cand = [[sx, sy - mr - g], [sx - 2 * mr - g, sy - mr], [sx + 2 * mr + g, sy - mr]];
      const ok = j => free(cand[j][0] - mr, cand[j][1] - mr, cand[j][0] + mr, cand[j][1] + mr);
      let slot = -1;
      if (m.slot >= 0 && ok(m.slot)) { slot = m.slot; m.blockAt = 0; }
      else {
        if (m.slot >= 0) { m.blockAt = m.blockAt || pnow; if (pnow - m.blockAt < TAG_BLOCK_MS) slot = m.slot; }
        if (slot < 0) { for (let j = 0; j < cand.length; j++) if (j !== m.slot && ok(j)) { slot = j; m.blockAt = 0; break; } }
        if (slot < 0) slot = m.slot >= 0 ? m.slot : 0;   // 빈 자리가 없으면 그대로(꼭대기)
      }
      if (m.slot >= 0 && slot !== m.slot && m.a > .05) { m.slideFrom = [m.px - cand[m.slot][0], m.py - cand[m.slot][1]]; m.slideAt = pnow; }   // 자리 옮김 — 미끄러진다(기둥 기준 상대 위치로)
      if (m.slot < 0) m.slideAt = 0;
      m.slot = slot;
      const k = m.slideAt ? Math.min(1, (pnow - m.slideAt) / TAG_SLIDE_MS) : 1, e = k * k * (3 - 2 * k);
      const [tx, ty] = cand[slot];
      m.px = k < 1 ? sx + m.slideFrom[0] + (tx - sx - m.slideFrom[0]) * e : tx;
      m.py = k < 1 ? sy + m.slideFrom[1] + (ty - sy - m.slideFrom[1]) * e : ty;
      if (k >= 1) m.slideAt = 0;
      boxes.push([m.px - mr, m.py - mr, m.px + mr, m.py + mr]);
      const ea = m.a * m.a * (3 - 2 * m.a), bob = Math.sin(pnow / MARK_BOB_MS * Math.PI * 2 + c.x * .01) * mr * .18;
      v3.set(m.px / W * 2 - 1, -((m.py + bob + (1 - ea) * mr * .9) / H * 2 - 1), zN).unproject(cam);   // 아래에서 올라오며 나타나고 내려가며 사라진다
      m.g.position.copy(v3);
    }
    // 역 이름 — 2D 지도와 같은 기준(`stScale`: 축척 막대 값·`ST_T3_M`). 3D 는 자리마다 축척이 달라 글자 자리의 m/px 를 막대 값으로
    // 바꿔 가른다 — 그 자리가 1km 막대 안이면 모든 역, 밖이면 주요 역만(사용자 11-25). 나타나고 사라질 때는 이름표와 같은 걸음으로 옅어진다
    const SC = opt.stScale && opt.stScale.nice ? opt.stScale : null;
    // 축척은 화면 가운데(공전 중심) 하나로 본다 — 2D 가 화면 하나에 축척 막대 하나인 것과 같다. 글자 자리마다 재면 비스듬히 볼 때 카메라
    // 앞쪽(화면 아래)이 늘 1km 안으로 잡혀 그쪽 역이 다 나왔다(사용자 11-25 「그대로 다 나온다」)
    // 비스듬히 보면 앞뒤 방향 땅이 sin(내려다보는 각)만큼 눌린다 — 지도 1px 이 덮는 땅은 그 방향이 더 길다. 그 긴 쪽으로 축척을 잰다(기하, 새 값 아님)
    const gnd = (p, d) => d / Math.max(.05, Math.abs(cam.position.y - p.y) / Math.max(1, cam.position.distanceTo(p)));
    const mppC = gnd(ctl.target, 2 * cam.position.distanceTo(ctl.target) * tanH / H);
    grid.bar = SC ? SC.nice(mppC) : mppC * 100;   // 낮은 건물 칸도 같은 가운데 축척(막대 값)으로 고른다
    const allSt = SC ? SC.nice(mppC) <= SC.t3 : mppC <= T3_MPP;
    let nb = 0;
    for (const l of lbs) {
      pa.set(l.x, l.hy ? l.hy * bS + 30 : 0, l.z);
      const s = scr(pa);
      let on = !!s;
      if (on) {
        const mpp = 2 * cam.position.distanceTo(pa) * tanH / H;   // 그 글자 자리의 화면 축척(m/px)
        const mppG = gnd(pa, mpp);
        on = l.kind === 'b' ? nb < BLDG_LB_N && mpp <= T2_MPP
          : l.kind === 'a' ? mpp <= AREA_MPP
          : l.major || allSt && (SC ? SC.nice(mppG) <= SC.t3 : mppG <= T3_MPP);   // 가운데가 1km 안일 때만, 그 자리도 1km 안인 역(먼 지평선 쪽은 주요 역만)
      }
      let fit = false;
      if (s) {
        if (!l.w) { l.el.style.display = ''; l.w = l.el.offsetWidth; l.h = l.el.offsetHeight; if (!l.a) l.el.style.display = 'none'; }
        l.px = s[0] - l.w / 2; l.py = l.kind === 'b' ? s[1] - l.h - 4 : s[1] - l.h / 2;
        if (on && free(l.px - 3, l.py - 2, l.px + l.w + 3, l.py + l.h + 2)) { fit = true; boxes.push([l.px - 3, l.py - 2, l.px + l.w + 3, l.py + l.h + 2]); }
      }
      l.a = Math.max(0, Math.min(1, l.a + (fit ? 1 : -1) * pdt / TAG_FADE_MS));
      if (fit && l.kind === 'b') nb++;
      if (l.a <= .005 || !s) { if (l.shown !== false) { l.el.style.display = 'none'; l.shown = false; } continue; }
      if (l.shown !== true) { l.el.style.display = ''; l.shown = true; }
      const base = l.kind === 'a' ? (l.out ? .5 : .8) : l.kind === 'b' ? .7 : 1;   // CSS 의 옅기(지명·건물)를 inline 이 덮으므로 여기서 곱한다
      l.el.style.opacity = (base * l.a * l.a * (3 - 2 * l.a)).toFixed(3);
      l.el.style.transform = `translate(${l.px}px,${l.py}px)`;
    }
  };

  // ── 넓은 바닥·전환 ─────────────────────────────────────────────────────
  // 넓은 바닥은 3D 가 지도를 다 가린 뒤에 찍는다(지도 보기를 잠깐 그 범위로 옮겼다 되돌린다 — `rp3dWide`)
  const wideBox = { x: lon0 - G / KX, y: -my0 - G / KX, w: 2 * G / KX, h: 2 * G / KX };
  const startTilt = () => { if (phase !== 'top' || !alive) return; phase = 'tilt'; ph0 = performance.now(); };
  const loadWide = async () => {
    let cv = null;
    // 선 굵기 — 공전 시점(범위 지름 ≈ 화면 폭)에서 지도와 같은 굵기로 보이게: 화면 1px 의 m ÷ 그림 1px 의 m
    const kWide = (2.2 * R / Math.max(1, wrap.clientWidth)) / (2 * G / WIDE_PX);
    try { cv = opt.wide ? await opt.wide(wideBox, WIDE_PX, kWide) : null; } catch (e) { cv = null; }
    if (!alive) return;
    if (cv) { wide.material.map = texOf(cv); wide.material.needsUpdate = true; }
    else wide.material.color.set(mapBg);
    wide.visible = true;
    startTilt();
    // 셀 장애 — 낮게 비스듬히 보므로 넓은 바닥은 거칠다. 범위 둘레를 한 장 더 촘촘히 찍어 위에 깐다(사용자 11-25 「도심이 안 보인다」)(기울이는 동안 찍는다 — 지도는 3D 뒤에 가려져 있다)
    if (!onRoute && opt.wide) {
      const L = R * LOCAL_PLATE_K, lb = { x: lon0 - L / 2 / KX, y: -my0 - L / 2 / KX, w: L / KX, h: L / KX };
      let lc = null;
      try { lc = await opt.wide(lb, WIDE_PX, (2.2 * R / Math.max(1, wrap.clientWidth)) / (L / WIDE_PX)); } catch (e) { lc = null; }
      if (!alive || !lc) return;
      const lp = plate(L, L, 0, 0, -2.5); lp.material.map = texOf(lc); lp.material.needsUpdate = true;
    }
  };

  let raf = 0;
  const loop = now => {
    raf = requestAnimationFrame(loop);
    const dt = Math.min(100, now - lastNow); lastNow = now;
    // 분 넘기기 — 전환이 끝나면 시작. 한 분 시간은 구간마다 다르다(stepOf). 마지막 분 뒤 끝마무리
    if (playing && phase === 'run') {
      if (fi < 0) goTo(0);
      else if (outro) {
        outro.t += dt * speed;
        // 끝 — 하이라이트(가장 넓던 분)에 둔 채 계속 돈다. 멈추면 「뭘 해야 하나」 싶다(사용자 11-25) — 조작 안내를 띄운다
        if (outro.t >= OUT_HOLD_MS + OUT_PULL_MS) { ended = true; setPlay(false); showHint(true); }
      } else {
        tick += dt * speed;
        if (tick >= stepOf(fi)) { if (fi < last) goTo(fi + 1); else startOutro(); }
      }
    }
    // 끝마무리에 천천히 멀어진다 — 다시 재생하면 제자리로
    const zT = outro ? 1 + (OUT_ZOOM - 1) * ease(Math.max(0, Math.min(1, (outro.t - OUT_HOLD_MS) / OUT_PULL_MS))) : 1;
    if (outro || !ended) zoomK += (zT - zoomK) * (outro ? 1 : 1 - Math.exp(-dt / 600));
    if (phase !== 'back') { syncCap(now); syncTags(now); }
    gridTick(now, dt);
    // 표식 — 이름표 자리의 셀 기둥 위에 떠서 위아래로 흔들리며 카메라를 본다. 이름표와 같이 천천히 나타나고 사라진다
    for (const t of cellTags) {
      const m = t.mk; if (!m) continue;
      const want = t.k && !t.leave && t.cell && phase !== 'back' ? 1 : 0;
      m.a = Math.max(0, Math.min(1, m.a + (want ? 1 : -1) * dt / TAG_FADE_MS));   // 이름표와 같은 속도(고른 걸음)
      m.g.visible = m.a > .02 && !!t.cell;
      if (!m.g.visible) continue;
      const c = t.cell, sz = mSize * (t.sev ? 1 : .8);
      const ea = m.a * m.a * (3 - 2 * m.a);   // 자리(위치)는 place() 가 화면 배치로 정한다
      m.g.scale.setScalar(sz * (.85 + .15 * ea));
      m.g.quaternion.copy(cam.quaternion); m.g.rotateY(Math.sin(now / MARK_BOB_MS / 1.7 * Math.PI * 2 + c.z * .01) * .35);
      for (const [f, sd] of m.body) { f.color.copy(t.sev ? SEV : DEG); sd.color.copy(f.color).multiplyScalar(.5); }
      for (const x of m.mats) x.opacity = x.userData.op * ea;
    }
    // 열차 — 재생 시간과 따로 흐른다(B 구간이 느려져도 열차는 같은 걸음)
    for (const tr of trains) {
      const d = trainD(tr, now);
      tr.sp.forEach((s, k) => { const [x, z] = along(d - tr.dir * k * TAIL_GAP); s.position.set(x, 60, z); if (!k) { tr.x = x; tr.z = z; } });
    }
    // 기둥 — 높이·밝기·색을 부드럽게 따라가고, 열차가 지나면 잠깐 밝게
    const a = 1 - Math.exp(-dt / 180), fall = 1 - Math.exp(-dt / (FALL_MS / 3));
    const fadeAll = phase === 'back' ? 1 - Math.min(1, (now - ph0) / BACK_MS) : 1;
    for (const c of cells) {
      let e = 0;
      if (c.state) for (const tr of trains) { const d = Math.hypot(tr.x - c.x, tr.z - c.z); if (d < FLASH_M) e = Math.max(e, 1 - d / FLASH_M); }
      c.flash = Math.max(e, c.flash - dt / 600);
      const b = c.hT < c.h ? fall : a;   // 내려갈 때는 천천히
      c.h += (c.hT - c.h) * b; c.i += (c.iT - c.i) * b; c.col.lerp(c.colT, a);
      const on = c.h > 8 && c.i > .02 && fadeAll > 0;
      c.core.visible = c.halo.visible = c.glow.visible = on;
      if (!on) continue;
      const I = c.i * (1 + c.flash * (light ? .4 : 1.4)) * fadeAll;
      c.core.scale.y = c.halo.scale.y = c.h;
      tc.copy(c.col); if (!light) tc.lerp(FLASH, c.flash * .6);
      c.core.material.uniforms.uColor.value.copy(tc); c.core.material.uniforms.uI.value = I;
      c.halo.material.uniforms.uColor.value.copy(tc); c.halo.material.uniforms.uI.value = I * (light ? .22 : .4);
      const gw = (c.h > H_DEG * hkH + 1 ? GLOW_SEV : GLOW_DEG) * hk;   // 심각 높이에 있는 동안은 넓게
      const gs = gw * (.6 + .4 * c.i) * (1 + c.flash * .35);
      c.glow.scale.set(gs, 1, gs); c.glow.material.color.copy(c.col);
      c.glow.material.opacity = Math.min(1, (.75 * c.i + c.flash * .3) * gk * (light ? .45 : 1) * fadeAll);
    }
    // 카메라
    if (phase === 'top') {
      offK = 0; pose(topC, dTop, EL_TOP, 0, cam.position); cam.lookAt(topC); ctl.target.copy(topC);
    } else if (phase === 'tilt') {
      const k = ease(Math.min(1, (now - ph0) / TILT_MS));
      pb.lerpVectors(topC, center, k);
      const d = Math.exp(Math.log(dTop) + (Math.log(dist) - Math.log(dTop)) * k);
      pose(pb, d, EL_TOP + (elRun - EL_TOP) * k, 0, cam.position); cam.lookAt(pb); ctl.target.copy(pb);
      offK = k; setOff();
      if (near) near.material.opacity = 1 - k;
      if (k >= 1) { phase = 'run'; ctl.enabled = true; auto = true; az = 0; if (near) near.visible = false; }
    } else if (phase === 'back') {
      // 가운데·거리(로그)·기울기·방위를 함께 풀어 축척이 자연스럽게 원래 지도 보기로 바뀐다(사용자 11-25 「3초, 축척도 같이」)
      const k = ease(Math.min(1, (now - ph0) / BACK_MS));
      pb.lerpVectors(from.q, topC, k);
      pose(pb, Math.exp(Math.log(from.d) + (Math.log(dTop) - Math.log(from.d)) * k), from.el + (EL_TOP - from.el) * k, from.az * (1 - k), cam.position);
      cam.lookAt(pb);
      offK = from.off * (1 - k); setOff();
      if (near) { near.visible = true; near.material.opacity = k; }
    } else if (back) {
      const k = ease(Math.min(1, (now - back.t) / 1200));
      pose(center, dist, elRun, az, pa); cam.position.lerpVectors(back.p0, pa, k); ctl.target.lerpVectors(back.q0, center, k); cam.lookAt(ctl.target);
      if (k >= 1) { back = null; auto = true; }
    } else if (auto) {
      if (playing || ended) az += dt / LAP_CAM_MS * Math.PI * 2;
      pose(center, dist * zoomK, elRun, az, cam.position);
      ctl.target.copy(center); cam.lookAt(center);
    } else ctl.update();
    if (fog) {   // 먼 바닥 안개 — 기우는 만큼 켜고 닫을 때 끈다(지도와 맞춘 시점에서는 안개가 없어야 지도와 같다)
      const fk = phase === 'top' ? 0 : phase === 'tilt' ? ease(Math.min(1, (now - ph0) / TILT_MS)) : phase === 'back' ? 1 - ease(Math.min(1, (now - ph0) / BACK_MS)) : 1;
      const D = dist * zoomK;
      fog.near = fk > .01 ? D * FOG_K[0] / fk : 1e9; fog.far = fk > .01 ? D * FOG_K[1] / fk : 2e9;
    }
    renderer.render(scene, cam);
    // 화면 글자·셀 상자
    const W = wrap.clientWidth, H = wrap.clientHeight;
    place(W, H);
    if (tip) {
      const c = tip.cell; v3.set(c.x, Math.max(c.h, 200), c.z).project(cam);
      const vis = v3.z < 1;
      tip.el.style.display = vis ? '' : 'none';
      if (vis) { const x = (v3.x + 1) / 2 * W, y = (1 - v3.y) / 2 * H; tip.el.style.transform = `translate(${Math.min(W - 340, Math.max(8, x + 12))}px,${Math.max(60, y - 20)}px)`; }
    }
  };

  // 닫기 — 위에서 본 지도 보기로 돌아온 뒤 교차 페이드로 지도에 넘긴다
  closing = () => {
    closeTip();
    for (const t of tags) t.on = false;
    if (phase === 'top') { wrap.classList.add('pre'); panels('off'); setTimeout(finish, XFADE_MS); return; }
    const q = ctl.target.clone(), d0 = Math.max(1, cam.position.distanceTo(q));
    let az0 = Math.atan2(cam.position.x - q.x, cam.position.z - q.z); az0 = Math.atan2(Math.sin(az0), Math.cos(az0));   // 가까운 쪽으로 돈다
    from = { q, d: d0, el: Math.asin(Math.max(-1, Math.min(1, (cam.position.y - q.y) / d0))) / RAD, az: az0, off: offK };
    showHint(false);
    phase = 'back'; ph0 = performance.now(); ctl.enabled = false;
    setTimeout(() => { wrap.classList.add('pre'); panels('off'); setTimeout(finish, CLOSE_XFADE_MS); }, BACK_MS);
  };
  teardown = () => {
    cancelAnimationFrame(raf); ro.disconnect(); clearTimeout(faTimer);
    window.removeEventListener('pointermove', onBarMove); window.removeEventListener('pointerup', onBarUp);
    gl.removeEventListener('pointerdown', onDown); gl.removeEventListener('pointerup', onUp);
    ctl.dispose();
    for (const o of disposables) o.dispose && o.dispose();
    renderer.dispose(); renderer.forceContextLoss && renderer.forceContextLoss();
  };

  // 첫 그림(지도와 같은 시점)을 그린 뒤 교차 페이드 — 지도 글자도 이때 끈다(3D 글자가 같은 자리에 뜬다)
  $('.r3d-msg').remove();
  ready = true;
  raf = requestAnimationFrame(loop);
  requestAnimationFrame(() => requestAnimationFrame(() => {
    if (!alive) return;
    wrap.classList.remove('pre'); panels('lab');
    setTimeout(() => { if (alive) loadWide(); }, XFADE_MS + 50);
  }));
  return st;
}

function esc(v) { return String(v == null ? '' : v).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
