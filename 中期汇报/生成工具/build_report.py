"""Build the deck and an external architecture image from verified project facts.

Run: PYTHONPATH=/tmp/nms-midterm-tools python3 生成工具/build_report.py
The diagram is rasterized here and embedded as one image, never PPT shapes.
"""
from pathlib import Path
from speaker_notes import write_guide
from datetime import datetime
import json, math, hashlib
from xml.sax.saxutils import escape
from PIL import Image,ImageDraw,ImageFont
from pptx import Presentation
from pptx.util import Inches,Pt
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.oxml.xmlchemy import OxmlElement

ROOT=Path(__file__).resolve().parents[1]
ASSETS=ROOT/'素材'
REVIEW=ROOT.parent/'project/data/midterm-review'
FONT='/mnt/c/Windows/Fonts/msyh.ttc'
BOLD='/mnt/c/Windows/Fonts/msyhbd.ttc'
NAVY='12304B'; TEAL='087F96'; INK='193A50'; MUTED='557083'; BG='F3F7FA'; LINE='DCE6ED'; GOLD='C37E24'

def architecture():
    im=Image.new('RGB',(2400,1500),'white');d=ImageDraw.Draw(im)
    svg=['<svg xmlns="http://www.w3.org/2000/svg" width="2400" height="1500" viewBox="0 0 2400 1500"><rect width="2400" height="1500" fill="white"/>']
    def rect(x,y,w,h,fill,outline=LINE,r=22):
        d.rounded_rectangle((x,y,x+w,y+h),radius=r,fill='#'+fill,outline='#'+outline,width=3)
        svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="#{fill}" stroke="#{outline}" stroke-width="3"/>')
    def txt(x,y,text,size=34,color=INK,bold=False):
        font=ImageFont.truetype(BOLD if bold else FONT,size)
        d.text((x,y),text,font=font,fill='#'+color,anchor='lt')
        svg.append(f'<text x="{x}" y="{y+size}" font-family="Microsoft YaHei, sans-serif" font-size="{size}" font-weight="{700 if bold else 400}" fill="#{color}">{escape(text)}</text>')
    def box(x,y,w,h,title,lines):
        rect(x,y,w,h,'FFFFFF');txt(x+24,y+24,title,40,NAVY,True)
        for i,line in enumerate(lines):txt(x+24,y+87+43*i,line,31)
    def arrow(points,color=TEAL):
        d.line(points,fill='#'+color,width=6,joint='curve')
        a,b=points[-2:];ang=math.atan2(b[1]-a[1],b[0]-a[0])
        triangle=[b,(b[0]-20*math.cos(ang-.42),b[1]-20*math.sin(ang-.42)),(b[0]-20*math.cos(ang+.42),b[1]-20*math.sin(ang+.42))]
        d.polygon(triangle,fill='#'+color)
        svg.append(f'<polyline points="{" ".join(f"{x},{y}" for x,y in points)}" fill="none" stroke="#{color}" stroke-width="6"/>')
        svg.append(f'<polygon points="{" ".join(f"{x},{y}" for x,y in triangle)}" fill="#{color}"/>')
    rect(40,30,2320,300,'EFF7FA');txt(75,51,'维护电脑 · Windows',34,TEAL,True)
    box(90,112,620,170,'浏览器网页',['原生 HTML / CSS / JavaScript','五项管理功能、趋势图、CSV'])
    box(880,112,660,170,'本机授权网关',['127.0.0.1:8000 · DPAPI 加密凭据','浏览器不持有真实管理令牌'])
    box(1710,112,600,170,'SSH 转发与 WARP',['仅回环监听 · 固定主机密钥','新电脑通过 WARP 代理接入'])
    arrow([(710,196),(880,196)]);arrow([(1540,196),(1710,196)])
    arrow([(2010,282),(2010,377),(405,377),(405,525)])
    txt(900,343,'加密通道 → DEV 127.0.0.1:8000',32,TEAL)
    rect(40,435,2320,600,'F3F7FA');txt(75,453,'DEV · 主应用单 worker + 独立被动监听服务',34,TEAL,True)
    box(90,525,620,235,'网页与 API',['静态资源、查询与 CSV 导出','写操作校验令牌与功能开关','查询读取数据库，不触发 SNMP'])
    box(880,525,660,235,'SQLite · schema v4',['原始观测、指标、快照、任务','故障 / 风险及其证据与状态','来源、时间、单位随记录保存'])
    box(1710,525,600,235,'采集与管理逻辑',['后台轮询、任务锁、失败隔离','故障 / 配置 / 性能 / 计费 / 安全','受控 iperf3 启停与清理'])
    arrow([(880,625),(710,625)]);txt(735,566,'读取',29,TEAL)
    arrow([(1710,625),(1540,625)]);txt(1570,566,'写入',29,TEAL)
    box(90,800,960,180,'被动扫描监听 · 独立 systemd 服务',['仅 CAP_NET_RAW；读取本机入站 TCP SYN','来源 IP / 目标 IP / 端口 / 时间 → 聚合窗口与风险'])
    arrow([(1050,890),(1210,890),(1210,760)]);txt(1280,870,'只存元数据，不保存报文内容',30,TEAL)
    txt(1280,925,'主应用仍为普通用户，无抓包权限',30,TEAL)
    rect(40,1110,2320,335,'EFF7FA');txt(75,1128,'课程实验网 · 已登记目标 / 发往 DEV 的入站连接',34,TEAL,True)
    box(90,1190,1060,180,'路由候选地址 × 4',['SNMP v2c：接口、路由、IP / 掩码、计数器','课程未开放 SET / TRAP；候选数不等于物理设备数'])
    box(1280,1190,1030,180,'测试主机 × 3',['ping / 指定端口；受控 iperf3 仅 host-1、host-2','被动监测只覆盖 DEV 三个本机地址，不覆盖全网'])
    arrow([(1930,760),(1930,1060),(620,1060),(620,1190)]);txt(760,1073,'SNMP / ping / 受控任务',30,TEAL)
    arrow([(1930,1060),(2180,1060),(2180,1190)]);arrow([(210,1110),(210,980)]);txt(250,1045,'入站 SYN',30,TEAL)
    im.save(ASSETS/'项目架构图.png');svg.append('</svg>');(ASSETS/'项目架构图.svg').write_text('\n'.join(svg),encoding='utf-8')

prs=Presentation();prs.slide_width=Inches(13.333);prs.slide_height=Inches(7.5)
manifest=[]
def speaker_for(page):
    return '蒋天乐' if page<=4 else '金海' if page<=7 else '元泽瑞' if page<=10 else '段景瀚'

def color(v):return RGBColor.from_string(v)
def shape(slide,x,y,w,h,fill,line=None,radius=False):
    s=slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE,Inches(x),Inches(y),Inches(w),Inches(h))
    s.fill.solid();s.fill.fore_color.rgb=color(fill)
    if line:s.line.color.rgb=color(line)
    else:s.line.fill.background()
    if radius:s.adjustments[0]=.12
    return s
def text(slide,x,y,w,h,content,size=22,fill=INK,bold=False):
    tb=slide.shapes.add_textbox(Inches(x),Inches(y),Inches(w),Inches(h));tf=tb.text_frame
    tf.clear();tf.word_wrap=True;tf.margin_left=0;tf.margin_right=0;tf.margin_top=0;tf.margin_bottom=0
    for i,line in enumerate(content.split('\n')):
        p=tf.paragraphs[0] if i==0 else tf.add_paragraph();p.text=line;p.font.name='Microsoft YaHei';p.font.size=Pt(size);p.font.bold=bold;p.font.color.rgb=color(fill);p.space_after=Pt(3)
        for run in p.runs:
            rpr=run._r.get_or_add_rPr();ea=OxmlElement('a:ea');ea.set('typeface','Microsoft YaHei');rpr.append(ea)
    return tb
def base(title,kicker='当前工作进展'):
    s=prs.slides.add_slide(prs.slide_layouts[6]);s.background.fill.solid();s.background.fill.fore_color.rgb=color(BG)
    shape(s,0,0,13.333,.09,TEAL);text(s,.5,.28,11,.3,kicker,13,TEAL,True)
    text(s,.5,.75,12.3,.6,title,30,NAVY,True)
    shape(s,.5,7.08,12.33,.012,LINE)
    text(s,.5,7.19,9.3,.2,'第 2 组 · 网络管理课程设计中期汇报  |  2026.11.15',10,MUTED)
    text(s,10.1,7.16,2.05,.3,f'主讲人：{speaker_for(len(prs.slides))}',12,TEAL,True)
    text(s,12.25,7.16,.5,.3,f'{len(prs.slides):02d}',12,MUTED)
    return s
def notes(slide,seconds,body,sources,assets=()):
    speaker=speaker_for(len(prs.slides))
    content=f'主讲人：{speaker}｜建议用时：{seconds} 秒。时间含指图与转场。\n\n讲解：\n{body}\n\n事实来源：\n'+ '\n'.join(sources)
    slide.notes_slide.notes_text_frame.text=content
    manifest.append({'slide':len(prs.slides),'speaker':speaker,'title':next(sh.text for sh in slide.shapes if sh.has_text_frame and sh.top==Inches(.75)) if len(prs.slides)>1 else '真实实验网络管理系统','seconds':seconds,'notes':content,'assets':list(assets)})
def picture(slide,file,x,y,w,h):
    width,height=Image.open(file).size;r=min(w/width,h/height)
    return slide.shapes.add_picture(str(file),Inches(x+(w-width*r)/2),Inches(y+(h-height*r)/2),width=Inches(width*r),height=Inches(height*r))
def bullets(slide,x,y,w,items,size=21,step=.91):
    for i,(head,body) in enumerate(items):
        text(slide,x,y+i*step,w,.36,head,size,TEAL,True)
        text(slide,x,y+.44+i*step,w,1.0,body,size-4,INK)
def screenshot_slide(title,file,items,tag='2026-11-15 当前网页截图',kicker='当前工作进展'):
    s=base(title,kicker);shape(s,.48,1.55,8.65,5.35,'FFFFFF',LINE,True)
    picture(s,ASSETS/file,.56,1.63,8.49,5.18)
    text(s,9.48,1.65,3.27,.55,tag,12,MUTED)
    bullets(s,9.48,2.3,3.27,items,21,1.43)
    return s
def table(slide,headers,rows,widths,y=1.65,rowh=.83,size=19):
    x=.55
    for head,w in zip(headers,widths):
        shape(slide,x,y,w,.55,NAVY);text(slide,x+.14,y+.12,w-.28,.35,head,size,'FFFFFF',True);x+=w
    for i,row in enumerate(rows):
        x=.55
        for value,w in zip(row,widths):
            shape(slide,x,y+.55+i*rowh,w,rowh,'FFFFFF' if i%2==0 else 'EAF2F6',LINE)
            text(slide,x+.14,y+.68+i*rowh,w-.28,rowh-.16,value,size,INK);x+=w

architecture()
s=prs.slides.add_slide(prs.slide_layouts[6]);s.background.fill.solid();s.background.fill.fore_color.rgb=color(NAVY)
shape(s,.55,.62,.12,5.9,TEAL);text(s,.95,.8,10,.4,'网络管理课程设计 · 第 2 组',19,'8FDEEA',True)
text(s,.95,1.95,11,1.6,'真实实验网络\n管理系统',43,'FFFFFF',True)
text(s,.98,4.2,11,.55,'中期汇报：设计方案、实现进展、问题与后续计划',25,'D6E9F2')
text(s,.98,5.25,11,.8,'蒋天乐 · 金海 · 元泽瑞 · 段景瀚\n2026 年 11 月 15 日',19,'D6E9F2')
text(s,10.1,7.16,2.05,.3,f'主讲人：{speaker_for(1)}',12,'8FDEEA',True)
notes(s,25,'大家好，我们是第二组。本次汇报围绕真实实验网中的网络管理系统展开。先介绍已经部署的系统和设计，再通过当前网页说明五项管理功能，最后汇报实际遇到的问题和下一阶段计划。页面截图采于十一月十五日；其中历史测速和流量统计会单独标出原始时间。',['教师中期汇报要求；项目当前源码与本轮浏览器记录'])

s=screenshot_slide('系统已经部署到 DEV，网页可读取持续采集结果','overview.png',[
    ('8 个登记候选','DEV、3 台测试主机；\n4 个路由候选地址。'),
    ('48 条接口记录','按 4 个候选地址分别观测；\n当前保留 92 条路由记录。'),
    ('新增被动扫描监测','安全页记录入站扫描线索；\n仅覆盖 DEV 本机地址。')])
notes(s,65,'这张图是本次检查时实际打开的网页。系统已经部署在 DEV，浏览器关闭后后台仍会采集。概览显示八个登记候选和四十八条接口记录。这里要区分候选地址和物理设备，不能把几个地址直接解释为几台独立路由器。页面显示的四条接口告警与虚拟网桥有关，业务影响还需要核实。网页刷新只是读取已有记录，采样时间另行显示。我们已经走通采集、存储、API 和网页展示这条链路。概览截图保留升级前的当日状态，新增扫描证据在第十一页单独展示，不能混用两个时刻的风险数量。',['本轮 browser-results.json：overview、health；截图中的来源与时间'],['overview.png'])

s=base('五项课程功能已覆盖，验收深度仍有边界')
table(s,['课程要求','当前实现','仍需补齐'],[
    ['故障管理','连通性 / 接口检测，诊断与告警','真实故障发生到恢复案例'],
    ['配置管理','IP、掩码、路由、逻辑拓扑与快照','真实变化验证、设备身份核实'],
    ['性能管理','RTT、丢包、接口速率、受控测速','第三台主机测速授权'],
    ['计费管理','接口总量、测试网元对统计、CSV','全部业务的网元对流量'],
    ['安全管理','配置风险、基线、负载与被动扫描','全网覆盖、漏洞验证及身份归因'],
],[1.7,5.25,5.28],rowh=.84,size=19)
text(s,.6,6.64,12,.3,'当前定位：具备可演示的真实系统；功能实现与完整现场验收分别记录。',16,TEAL,True)
notes(s,60,'老师要求五项管理功能。当前五项均有代码和网页入口，也都有对应的数据处理。中期阶段不能用一个完成百分比掩盖差异：例如故障逻辑已有本地测试，但真实故障恢复案例不足；计费已经能够按接口统计，却没有完整的业务流记录。安全模块新增被动 TCP 扫描监测，已经记录来源 IP、目标和端口窗口；但还不能覆盖全网、确认攻击者身份或验证具体漏洞。后面会逐项说明已做到什么、依据是什么，以及下一步要补什么。',['project/docs/课程设计要求.md；当前模块代码；已有实测验收说明'])

s=base('架构：浏览器访问 DEV，采集与管理逻辑在服务端运行')
picture(s,ASSETS/'项目架构图.png',.5,1.48,12.33,5.48)
notes(s,100,'从上往下看，最上层是维护电脑。浏览器访问本机网关，本机网关从 DPAPI 加密凭据中恢复授权，真实管理令牌不会交给网页。连接通过固定主机密钥的 SSH 转发到 DEV，新电脑需要 WARP 提供课程入口的网络接入。中间层是 DEV 上由 systemd 管理的 FastAPI 服务，网页请求从 SQLite 读取记录，后台采集器和五项管理逻辑把结果写入数据库。主应用保持单 worker；新增被动监听是独立 systemd 服务，只授予原始套接字权限，把入站 SYN 元数据按窗口写入 schema 四的数据库，网页进程不获得抓包权限。最下面是实验对象。路由候选使用只读 SNMP 读取接口、路由和计数器；测试主机提供 ping、指定端口检查及受控 iperf3。课程未开放 SNMP SET 和 TRAP，所以系统没有远程修改设备配置或依赖主动通知。',['app/main.py；app/service.py；app/core/db.py；deploy/local_gateway.py；deploy/nms-scan-sensor.service；docs/被动扫描监测.md'],['项目架构图.png'])

s=base('采集与计算：保留原始依据，再生成指标和状态')
for x,n,title,body in [(.6,'01','观测','SNMP / ping / 受控测速\n本机入站 TCP SYN 元数据'),(4.83,'02','计算','速率 = 8 × 字节差 ÷ 时间差\n告警、快照与风险规则'),(9.06,'03','呈现','来源、采样时间、有效性\n图表、告警证据和 CSV')]:
    shape(s,x,1.75,3.67,2.68,'FFFFFF',LINE,True);text(s,x+.22,1.96,3,.4,n,22,TEAL,True);text(s,x+.22,2.57,3,.55,title,26,NAVY,True);text(s,x+.22,3.24,3.23,1.12,body,18)
shape(s,.6,4.85,12.13,1.56,'E2F0F4',None,True)
text(s,.85,5.08,11.6,.45,'15 秒是轮次结束后的等待时间，不是实际采样周期',23,TEAL,True)
text(s,.85,5.72,11.55,.5,'实际间隔还包含采集耗时；未知、复位和采样缺口不当作零值，也不连成有效趋势。',20)
notes(s,60,'这页说明数据为什么不能只看一个数字。系统先保留原始观测，再用相邻计数器和实际时间差计算速率。首次采样、重启、计数器复位或间隔过长时，数据会标成无效，不把它们当作正常的零流量。后台配置中的十五秒是每轮结束后的等待，实际开始到开始的间隔还要加上采集耗时。扫描监听独立于这套轮询，持续接收入站 SYN，再按六十秒或十五分钟固定窗口聚合。页面还区分文档、样例和真实采集。这样的处理能够让告警、趋势和报表回到原始依据，而不是只留下一个看起来合理的结果。',['app/service.py；app/modules/perf_accounting.py；接口与数据合同.md'])

s=screenshot_slide('故障管理：从告警进入证据与排查建议','fault.png',[
    ('连续异常才告警','连通性或接口连续 3 次异常；\n查询失败保持“未知”。'),
    ('确认与恢复分开','确认表示已经查看；\n恢复依赖后续有效观测。'),
    ('当前边界','virbr0 的业务影响待核实\n尚缺真实故障恢复案例')])
notes(s,65,'故障页同时提供设备筛选、告警和诊断依据。连续三次完整丢包，或者接口连续三次管理开启但运行关闭，才会产生对应事件。单次查询失败不会直接被认定为故障。点击详情可以查看关联的历史样本；确认只表示已经看过，不能把事件变为恢复。目前页面上的四条告警与 virbr0 虚拟网桥有关，因此我们明确标注业务影响待确认。诊断会提供相近时刻、同网段的异常线索，但不会直接宣称找到了根因。',['本轮故障页与故障详情；fault_config.py；diagnostics.py'],['fault.png'])

s=screenshot_slide('配置管理：拓扑关系带有来源和观测依据','topology.png',[
    ('发现 IP 与掩码','4 个候选共 27 条地址记录；\n保留接口、掩码及采样时间。'),
    ('两类关系分开展示','23 条采样子网关系；\n7 条课程文档关系。'),
    ('拓扑的解释范围','同一子网不证明物理直连；\n候选地址不自动归并。')])
notes(s,65,'配置管理已经能够读取接口、IPv4 地址与掩码、路由，并生成配置快照。图中的实线来自采样子网关系，虚线来自课程文档，这两类依据分开保存和展示。当前四个路由候选共二十七条地址记录，图中有二十三条采样关系和七条文档关系。需要强调，地址属于同一子网只支持逻辑关系，不能证明物理直连。快照比较功能能够报告新增、删除和变化，不过目前还缺真实网络配置发生变化后的完整验收案例，未采集的字段也不会误判成删除。',['本轮 topology.png、地址表；discovery.py；配置快照比较结果'],['topology.png'])

s=screenshot_slide('性能管理：时延、丢包与接口方向速率可追溯','performance.png',[
    ('按主机查看趋势','展示 RTT 与丢包率；\n可回看原始采样明细。'),
    ('按接口查看速率','基于 64 位字节计数差分；\n参考速率未知时不算利用率。'),
    ('当前观测','最近一轮 3 台主机：\n均 ping 成功，丢包率 0%')])
notes(s,60,'性能页把端到端指标与接口指标分开。上半部分是某一测试主机的往返时延和丢包趋势，下半部分按接口展示入、出方向速率。折线保留缺口，不会跨过无效数据连线。我们本次读取的最近一轮采集中，三个测试主机都有成功的 ping，丢包率为零。这只能描述当前观测，不能说明所有时间都没有故障。接口速率来自计数差，和主动测速得到的吞吐量含义不同，下一页展示主动测试的结果。',['本轮性能页、collector-runs API；perf_accounting.py'],['performance.png'])

s=screenshot_slide('网页测速已打通，历史成功结果可回看','iperf.png',[
    ('测试条件','host-1 → host-2\n2 秒，目标速率 10 Mbit/s。'),
    ('2026-10-17 实测','接收约 9.94 Mbit/s；\n接收 2,486,216 byte。'),
    ('控制与限制','任务限时、限速并清理进程；\n结果不代表最大链路容量。')],tag='当前网页中的 2026-10-17 历史任务')
notes(s,55,'这页是当前网页中保留的真实历史任务，测量发生在十月十七日，而不是本次截图当天。客户端是第一台主机，服务端是第二台，运行两秒，目标速率十兆比特每秒，接收结果约为九点九四兆比特每秒。任务会启动服务端、确认就绪、运行客户端并清理测试进程。系统还限制持续时间、目标速率和端点并发，避免测试失控。目前服务只允许前两台主机，第三台还没有配置专用授权。限速实验验证的是任务链路和结果处理，不是测量最大容量。',['本轮 tasks API；历史任务 a5eafd4c-a1b2-41fa-8756-34028dbb2c65；iperf_tasks.py'],['iperf.png'])

s=screenshot_slide('计费管理：接口总量与测试网元对分别统计','accounting-history.png',[
    ('接口 r-0-1:3','入 1,006,844 byte\n出 3,502,466 byte'),
    ('测试网元对传输量','host-1 → host-2\n接收 2,486,216 byte。'),
    ('范围不同，不相加','接口含经过该口的业务；\n网元对目前只含成功测速。')],tag='历史窗口：2026-10-17 21:55:38–21:56:24')
notes(s,75,'计费页使用同一个时间窗生成两类报表。左图查询的是十月十七日晚的历史窗口。接口总量表示经过观测接口的入、出方向字节；下面的网元对统计只汇总完整落在窗口内的成功 iperf3 任务。两者不是同一测量范围，既不能相加，也不能要求相等。报表会同时给出覆盖率、无效区间和来源，并导出同一窗口的 CSV。当前最大缺口是缺少普通业务的流记录，因此我们还不能回答任意主机对在整个网络中的全部业务流量。下一步首先要确认老师接受的统计粒度，再决定是否接入额外流观测。',['本轮历史窗口查询与 CSV 核对；perf_accounting.py：successful_iperf3_tasks_only'],['accounting-history.png'])

s=screenshot_slide('安全管理：已上线被动扫描监测，保留来源与证据','security-scan.png',[
    ('真实命中记录','192.168.3.1 → DEV\n60 秒 ≥39 个 TCP 端口'),
    ('监测方式','入站 SYN 按窗口聚合\n保存来源、端口与时间'),
    ('明确覆盖边界','覆盖 DEV 三个本机地址\n内网来源，身份尚待核实')],tag='2026-11-15 10:35 真实网页\n记录窗口：10:24 / 10:29')
notes(s,60,'安全模块已新增被动扫描监测。它监听到达 DEV 的 TCP SYN，按来源、目标和时间窗统计不同端口。六十秒至少八个端口会形成疑似扫描记录；十五分钟至少二十个端口且跨三个活跃分钟，也会触发。图中两次真实窗口都记录到课程内网的一九二点一六八点三点一访问 DEV，一分钟至少三十九个端口。这证明存在扫描特征，不能认定是公网攻击或已经入侵，也没有达到连续三分钟命中的证明。当前只覆盖 DEV 三个本机地址，默认 SNMP 等原有风险规则仍保留。告警可以查看证据和确认，确认不等于修复。',['project/docs/被动扫描监测.md；scan-review/browser-results.json、remote-final.json；scan_detection.py'],['security-scan.png'])

s=base('运行改进：网页可恢复，监听服务可独立运行','当前工作进展 · 可用性修正')
table(s,['实际问题','修正后的行为','验证方式'],[
    ['请求停滞时，刷新可能一直等待','读取 30 秒、写入 75 秒超时提示\n恢复操作入口；写操作不自动重试','响应头 / 正文停滞测试\n浏览器模拟后恢复真实刷新'],
    ['切换统计窗口时，旧结果可能混入','查询时清空旧表并显示等待\n旧请求不能覆盖新窗口及 CSV','延迟旧请求的并发回归\n真实历史窗口与 CSV 对照'],
    ['监听曾受无关报文与写锁影响','内核过滤 + 写锁非阻塞重试\n普通网页进程不增加抓包权限','120 项 Python / 4 组前端\n125 秒：零重启、零丢包'],
],[3.3,5.15,3.78],rowh=1.25,size=19)
text(s,.65,6.3,12,.42,'应用 0.4.0 / 数据库 v4 已部署 DEV；短时验证通过，仍需长期运行观察。',18,TEAL,True)
notes(s,65,'准备汇报时，我们实际检查了网页。第一个问题是请求没有截止时间，连接停滞可能让刷新一直等待。现在读取请求的响应头和正文都有三十秒上限，并给出重试提示。手动采集需要等待完整轮次，所以写请求保留七十五秒上限；超时只提示可能已经提交，不自动重发。第二个问题是流量查询的并发处理：旧查询返回得慢时，可能覆盖用户新选择的时间窗。现在查询开始先清空旧表，异步返回要核对请求编号，保证显示和 CSV 属于同一次查询。换机入口已恢复，但接入仍可能偶发断连。新监听服务还修复了无关报文拥塞和 SQLite 写锁阻塞：加入内核过滤，写锁忙时保留聚合结果并稍后重试。新增功能后共一百二十项 Python 和四组前端测试通过；最终一百二十五秒观察中零重启、零内核丢失，有十六次持久化重试。这是短时结果，不能替代长期负载验证。',['本轮 auth.js / app.js / alert-detail.js 修改；request_timeout_smoke.js；web_dom_smoke.js；deploy-results-final.json；docs/被动扫描监测.md；scan-review/stability-results.json'])

s=base('当前难点：观测边界、验收深度与长期运行','问题与难点')
table(s,['难点','为什么受限','处理方向'],[
    ['全部网元间业务流量','接口计数不能还原任意主机对','先确认粒度；再评估流记录来源'],
    ['扫描覆盖与身份归因','已记录来源 IP，但仅监测 DEV 入站','扩覆盖需观测点；身份需日志佐证'],
    ['真实变化与恢复案例','不能把样例或功能升级当现场变化','在获准场景下验证并保留前后证据'],
    ['持续采集与数据增长','历史指标已超过 238 万条','评估保留期、归档与查询耗时'],
],[3.0,4.8,4.43],rowh=1.02,size=19)
text(s,.65,6.48,12,.35,'接入层仍有偶发断连：本次汇报采用当前截图，并保留一键重连入口。',17,MUTED)
notes(s,70,'第一项难点是观测能力。接口总量不能凭空变成主机对流量，进一步统计需要流记录或等价数据。第二项是安全深度：已经能看到扫描来源 IP，但只覆盖 DEV 入站 TCP SYN。来源可能是授权测试，也可能被伪造；身份归因、全网覆盖与漏洞验证仍需额外证据和权限。第三项是现场案例，在受限实验环境中不能随意制造故障或改设备配置，需要获准的验证场景。第四项来自真实运行：当前历史指标已经超过二百三十八万条。数据确实在持续产生，但也意味着要评估保留周期、归档和查询耗时，这部分当前没有作为已完成能力。',['教师五项要求；当前源码边界；本轮 overview API 的 metric_samples 计数'])

s=base('后续计划：先明确验收，再补齐证据和交付','后续工作计划')
table(s,['阶段（建议）','具体工作','负责与验收产物'],[
    ['第 1 周\n明确验收口径','确认流量粒度、安全深度与最终期限\n核实设备身份和测量权限','蒋天乐牵头，全组参与\n形成明确的验收范围清单'],
    ['第 2 周\n补现场案例','获准故障 / 配置变化、扫描解除验证\n确认扩展观测点与第三主机授权','金海 / 元泽瑞 / 段景瀚按模块\n保留前后截图、API 与原始证据'],
    ['第 3 周\n稳定与交付','监听长期观察、断流与写锁恢复验证\n评估数据保留、更新教程并彩排','蒋天乐集成，全组复核\n可重复启动、演示、回归与交接'],
],[2.25,5.85,4.13],rowh=1.35,size=19)
text(s,.65,6.43,12,.32,'以上为中期后建议安排；具体日期以教师最终截止时间和组内确认结果为准。',16,MUTED)
notes(s,80,'后续按三个阶段推进，这是建议安排，不把它当作老师已经确认的截止日期。第一阶段先明确计费和安全验收口径，同时核实设备身份和权限。第二阶段按模块补现场证据：金海继续故障与配置，元泽瑞继续性能和流量统计，段景瀚继续扫描解除、误报漏报与覆盖验证，蒋天乐负责整体集成。每个案例都要保留前后状态和原始依据，不能只有截图结论。第三阶段继续监听服务的长期稳定观察和断流恢复验证，处理数据增长、教程与验收表的版本统一，再做完整彩排和交接。现有职责分工沿用开题安排，具体排期需要结合教师最终期限确认。',['开题分工记录；课程要求对照；本轮发现；建议计划，尚未签认'])

s=base('希望交流：两项验收口径需要明确','问题交流')
shape(s,.65,1.85,12.03,1.7,'FFFFFF',LINE,True)
text(s,.95,2.12,11.4,.45,'01  网元间流量统计需要做到什么粒度？',27,TEAL,True)
text(s,.95,2.85,11.4,.5,'接口方向总量与受控测试网元对是否足够；是否必须覆盖全部普通业务流量？',21)
shape(s,.65,3.88,12.03,1.7,'FFFFFF',LINE,True)
text(s,.95,4.15,11.4,.45,'02  安全风险检测需要达到什么深度？',27,TEAL,True)
text(s,.95,4.88,11.4,.5,'配置风险与 DEV 扫描线索是否足够；是否要求全网覆盖、漏洞验证和身份归因？',21)
text(s,.95,6.08,11.6,.55,'当前已具备真实系统与演示链路，下一阶段重点补齐验收范围内的现场证据。',22,NAVY,True)
notes(s,35,'最后希望老师帮助明确两项口径。第一，网元间流量统计是否需要覆盖所有普通业务，还是接受接口方向总量加受控测试网元对统计。第二，安全部分已有配置风险和 DEV 扫描来源线索，是否还要求全网覆盖、漏洞验证或攻击者身份归因。明确这些之后，我们会按范围补充现场证据并完成交付。谢谢。',['教师课程要求；当前未解决的验收范围'])

total=sum(x['seconds'] for x in manifest)
assert len(prs.slides)==15 and total<1200,(len(prs.slides),total)
path=ROOT/'第2组_网络管理系统_中期汇报.pptx';prs.save(path)
write_guide(ROOT, manifest)
(ROOT/'生成工具/manifest.json').write_text(json.dumps({'slides':manifest,'seconds':total,'pptx_sha256':hashlib.sha256(path.read_bytes()).hexdigest()},ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'pptx':str(path),'slides':len(prs.slides),'seconds':total},ensure_ascii=False))
