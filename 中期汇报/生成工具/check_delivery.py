"""Audit the actual artifacts and independently recorded browser evidence."""
from pathlib import Path
from zipfile import ZipFile
import json,hashlib,re
from pptx import Presentation
ROOT=Path(__file__).resolve().parents[1]
WORK=ROOT.parent/'project/data/midterm-review'
manifest=json.loads((ROOT/'生成工具/manifest.json').read_text())
render=json.loads((ROOT/'生成工具/render-results.json').read_text(encoding='utf-8-sig'))
desktop=json.loads((WORK/'browser-desktop.json').read_text())
final=json.loads((WORK/'browser-final.json').read_text())
deck=ROOT/'第2组_网络管理系统_中期汇报.pptx'
pdf=deck.with_suffix('.pdf')
prs=Presentation(deck)
views={'overview','fault','config','performance','accounting','security','operations'}
assert len(prs.slides)==15 and manifest['seconds']==940 and manifest['seconds']<1200
assert render['slides']==15 and not render['overflow']
assert pdf.stat().st_mtime>=deck.stat().st_mtime
assert pdf.read_bytes().startswith(b'%PDF-')
guide=(ROOT/'中期汇报_讲解稿.md').read_text(encoding='utf-8')
assert len(re.findall(r'^## 第 ',guide,re.M))==15
assert guide.count('### 技术备注与可能追问（备查，不逐条朗读）')==15
assert not any('验证记录：' in item['title'] for item in manifest['slides'])
assert len(list((ROOT/'生成工具/预览').glob('slide[0-9][0-9].png')))==15
for target in re.findall(r'\]\(([^)]+)\)',guide):
    if not target.startswith(('https:','http:','#')):
        assert (ROOT/target.split('#')[0]).exists(),target
assert set(desktop['views'])==views
assert desktop['history_page2'].startswith('11–20') and desktop['csv_verified']
assert desktop['refresh_preserved_form'] and desktop['timeout_recovery']['real_refresh_recovered']
assert not desktop['exceptions']
assert final['passed'] and final['final_scripts_loaded'] and final['risk_detail_verified']
assert set(final['desktop_views'])==views and set(final['mobile_views'])==views
assert final['mobile_no_overflow'] and final['methods']==['GET'] and not final['exceptions']
assert final['auth']=='本机管理员已连接'
# Earlier browser records remain historical; verify current code against the later deployment.
scan_work=ROOT.parent/'project/data/scan-review'
scan=json.loads((scan_work/'browser-results.json').read_text())
remote=json.loads((scan_work/'remote-final.json').read_text())
stability=json.loads((scan_work/'stability-results.json').read_text())
assert scan['passed'] and scan['mobile_no_overflow'] and scan['live_heartbeat_advanced']
assert set(scan['views'])==views and scan['methods']==['GET'] and not scan['exceptions']
assert stability['passed']
assert stability['observations'][-1]['effective_state']=='running'
assert len({row['session_id'] for row in stability['observations']})==1
for name,digest in remote['sha256'].items():
    local=ROOT.parent/'project'/('deploy/'+name if name.endswith('.service') else name)
    assert hashlib.sha256(local.read_bytes()).hexdigest()==digest,name
assert (ROOT/'素材/security-scan.png').read_bytes()==(ROOT.parent/'project/docs/images/scan-monitoring/security-desktop.png').read_bytes()
assert manifest['slides'][10]['assets']==['security-scan.png']
assert '来源' in manifest['slides'][10]['notes'] and '三十九' in manifest['slides'][10]['notes']
assert 'schema v4' in (ROOT/'素材/项目架构图.svg').read_text()

for i,item in enumerate(manifest['slides']):
    assert item['notes'] in prs.slides[i].notes_slide.notes_text_frame.text
    assert item['notes'].split('讲解：\n')[1].split('\n\n事实来源：')[0] in guide
    assert f"## 第 {i+1:02d} 页｜{item['title']}" in guide
    assert (ROOT/'生成工具/预览'/f'slide{i+1:02d}.png').exists()
    for name in item['assets']:assert (ROOT/'素材'/name).exists()
    for shape in prs.slides[i].shapes:
        assert shape.left>=0 and shape.top>=0
        assert shape.left+shape.width<=prs.slide_width+1000 and shape.top+shape.height<=prs.slide_height+1000
assert len([shape for shape in prs.slides[3].shapes if shape.shape_type==13])==1
with ZipFile(deck) as z:
    embedded={hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist() if n.startswith('ppt/media/')}
    for item in manifest['slides']:
        for name in item['assets']:assert hashlib.sha256((ROOT/'素材'/name).read_bytes()).hexdigest() in embedded
report={'slide_count':15,'planned_seconds':940,'powerpoint_rendered':True,'text_overflow':0,
        'markdown_technical_note_blocks':15,'markdown_local_links_valid':True,
        'markdown_question_count':len(re.findall(r'^\d+\. \*\*',guide,re.M)),
        'architecture_embedded_as_one_image':True,'all_used_screenshots_embedded_unchanged':True,
        'speaker_notes_all_slides':True,'desktop_views':sorted(views),'mobile_views':sorted(views),
        'browser_csv_matches_historical_task':True,'timeout_then_real_refresh_verified':True,
        'production_files_match_scan_deployment_record':True,
        'scan_browser_passed':True,'scan_stability_record_passed':True,
        'scan_observation_connection_errors':stability['connection_errors'],
        'scan_observation_states':[row['effective_state'] for row in stability['observations']],
        'scan_screenshot_captured_at':scan['captured_at'],
        'historical_browser_evidence_preserved':True,'browser_js_exceptions':0,
        'incidental_http_failures':final['failed_responses'],
        'artifacts':{p.name:{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
                     for p in [deck,pdf,ROOT/'中期汇报_讲解稿.md',ROOT/'素材/项目架构图.png',ROOT/'素材/项目架构图.svg']}}
(ROOT/'生成工具/交付核对.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in report.items() if k!='artifacts'},ensure_ascii=False))
