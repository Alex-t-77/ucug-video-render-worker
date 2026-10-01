"""Rendu MP4 d'une vidéo HTML « Un Chiffre, Un Geste ».

Équivalent Python du script local render-video.mjs (déjà validé) :
1. capture headless de l'animation (Playwright, 432×768 logiques ×2,5 = 1080×1920) ;
2. transcodage MP4 H.264 · 30 fps · yuv420p ;
3. alignement des segments vocaux sur les timecodes de scènes (adelay + amix).

Aucune constante à maintenir : les durées de scènes sont lues dans le HTML
(variable globale SCENES), donc ce module fonctionne pour toutes les futures
vidéos du compte.
"""

import asyncio
import subprocess  # noqa: F401  (import explicite pour lisibilité)
from pathlib import Path
from typing import Callable, List, Optional, Tuple

# Style injecté avant la capture : masque tout sauf l'écran vidéo
# (titre, commandes, légendes…) et fait occuper à l'écran 9:16 tout le cadre.
MASK_CSS = """
  body { padding: 0 !important; background: #10241a !important; }
  .wrap { max-width: 100% !important; padding: 0 !important; margin: 0 !important; text-align: left !important; }
  h1, .sub, .controls, .progress, .chapters, .scene-label, .legend, .export-note { display: none !important; }
  .phone { width: 432px !important; height: 768px !important;
           max-width: none !important; aspect-ratio: auto !important;
           border-radius: 0 !important; box-shadow: none !important; margin: 0 !important; }
"""

# Lit les durées de scènes DANS le canvas (aucune constante côté worker).
_JS_TIMELINE = """
() => {
  if (typeof SCENES === 'undefined') { throw new Error('SCENES introuvable dans le HTML fourni'); }
  let t = 0; const starts = [];
  for (const s of SCENES) { starts.push(t); t += s.dur; }
  return { starts, total: t };
}
"""


async def run_ffmpeg(args: List[str]) -> None:
    """Exécute ffmpeg ; lève une RuntimeError lisible en cas d'échec."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            "ffmpeg a échoué (code %s) :\n%s"
            % (proc.returncode, err.decode("utf-8", "replace")[-3000:])
        )


async def capture_animation(
    html_path: Path,
    workdir: Path,
    progress: Optional[Callable[[str], None]] = None,
) -> Tuple[Path, List[float], float]:
    """Capture une boucle complète de l'animation.

    Retourne (chemin du webm brut, départs de scènes en s, durée totale en s).
    """
    from playwright.async_api import async_playwright  # import tardif (image Docker)

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 432, "height": 768},   # 9:16 logique
            device_scale_factor=2.5,                  # ×2,5 → sortie réelle 1080×1920
            record_video_dir=str(workdir),
            record_video_size={"width": 1080, "height": 1920},
        )
        page = await context.new_page()
        try:
            await page.goto(html_path.as_uri())
            await page.add_style_tag(content=MASK_CSS)
            timeline = await page.evaluate(_JS_TIMELINE)
            starts = [float(s) for s in timeline["starts"]]
            total = float(timeline["total"])
            if progress:
                progress("capture : %d scènes, %d s" % (len(starts), int(total)))

            await page.wait_for_timeout(1000)          # laisse l'autoplay démarrer
            if await page.locator("#replayBtn").count():
                await page.click("#replayBtn")         # repart précisément de 0:00

            # Une boucle + marge, seconde par seconde (pulse de progression).
            for _ in range(int(total) + 2):
                await page.wait_for_timeout(1000)
                if progress:
                    progress("capture")

            video = page.video
            await context.close()                      # finalise le fichier webm
            webm = Path(await video.path())
        finally:
            await browser.close()

    return webm, starts, total


async def transcode(
    webm: Path,
    out: Path,
    crf: int = 23,
    scale_filter: Optional[str] = None,
) -> None:
    """Webm → MP4 H.264 · 30 fps · yuv420p (compatible Instagram/TikTok)."""
    args = [
        "-y", "-i", str(webm), "-r", "30",
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-an",
    ]
    if scale_filter:
        # Fallback : ex. "scale=720:1280" si l'upload Drive exige un fichier plus léger.
        args += ["-vf", scale_filter]
    args.append(str(out))
    await run_ffmpeg(args)


async def mux_with_voice(
    video_in: Path,
    voice_paths: List[Path],
    starts: List[float],
    total: float,
    out: Path,
) -> None:
    """Aligne chaque segment vocal au timecode de sa scène, puis assemble.

    La vidéo muette est l'entrée 0 ; le segment i est l'entrée i+1.
    Chaque segment est décalé au départ de sa scène (adelay), tous les segments
    sont mélangés en une piste unique (amix sans normalisation).
    """
    inputs: List[str] = []
    for f in voice_paths:
        inputs += ["-i", str(f)]

    delays = []
    for i in range(len(voice_paths)):
        start_ms = int(round(starts[min(i, len(starts) - 1)] * 1000))
        delays.append("[%d:a]adelay=%d:all=1[a%d]" % (i + 1, start_ms, i))
    mix_in = "".join("[a%d]" % i for i in range(len(voice_paths)))
    filt = ";".join(delays) + ";" + mix_in + (
        "amix=inputs=%d:normalize=0[aout]" % len(voice_paths)
    )

    await run_ffmpeg([
        "-y", "-i", str(video_in), *inputs,
        "-filter_complex", filt,
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-t", str(total),
        "-movflags", "+faststart",
        str(out),
    ])


async def remux_faststart(video_in: Path, out: Path) -> None:
    """Recopie le MP4 muet en ajoutant +faststart (lecture web/Drive)."""
    await run_ffmpeg([
        "-y", "-i", str(video_in), "-c", "copy", "-movflags", "+faststart", str(out),
    ])
