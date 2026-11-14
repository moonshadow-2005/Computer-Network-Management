$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new()
$root=Split-Path -Parent $PSScriptRoot
$file=Join-Path $root '第2组_网络管理系统_中期汇报.pptx'
$pdf=Join-Path $root '第2组_网络管理系统_中期汇报.pdf'
$previews=Join-Path $PSScriptRoot '预览'
New-Item -ItemType Directory -Force -Path $previews | Out-Null
$app=$null;$deck=$null;$prior=0
try {
  $app=New-Object -ComObject PowerPoint.Application
  $prior=$app.Presentations.Count
  $deck=$app.Presentations.Open($file,-1,0,0)
  if($deck.Slides.Count -ne 15){ throw 'Expected 15 slides' }
  $deck.SaveAs($pdf,32)
  $overflow=@()
  foreach($slide in $deck.Slides){
    $slide.Export((Join-Path $previews ('slide{0:00}.png' -f $slide.SlideIndex)),'PNG',1920,1080)
    foreach($shape in $slide.Shapes){
      if($shape.HasTextFrame -and $shape.TextFrame.HasText){
        if($shape.TextFrame2.TextRange.BoundHeight -gt $shape.Height + 2){
          $overflow+=@{slide=$slide.SlideIndex;shape=$shape.Name;text=$shape.TextFrame.TextRange.Text}
        }
      }
    }
  }
  Get-ChildItem -Path $previews -Filter 'slide*.png' | Where-Object { $_.BaseName -match '^slide([0-9]+)$' -and [int]$Matches[1] -gt $deck.Slides.Count } | Remove-Item
  @{slides=$deck.Slides.Count;pdf=$pdf;overflow=$overflow} | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 (Join-Path $PSScriptRoot 'render-results.json')
  $deck.Close();$deck=$null
  if($prior -eq 0){$app.Quit()}
  Write-Output ('Rendered 15 slides; overflow candidates: '+$overflow.Count)
} finally {
  if($null -ne $deck){$deck.Close()}
  if($null -ne $app){[System.Runtime.InteropServices.Marshal]::ReleaseComObject($app)|Out-Null}
}
