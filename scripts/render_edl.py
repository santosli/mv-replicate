#!/usr/bin/env python3
"""Render an edit decision list (EDL JSON) into an MV with ffmpeg.

EDL fields (relative paths resolve against --root, default: the current directory; ~ is expanded):
  song, duration, size [w, h], fps, grade (ffmpeg filter for footage, optional),
  loudness {I, TP} (optional; flat gain + peak limiter, audio rendered to WAV then muxed), end_fade (s), work_dir, out
  segments: contiguous list covering [0, duration]; each
    {at, dur, src | color, from | end, speed=1,
     layout="full" | {frame: "vertical", pos, bg, slide} | {frame: "photo", bg, scale, rot},
     flash_in=0, shake=0, punch=0, reverse=false, duotone=[shadow, (mid,) highlight], zoom=[z0, z1],
     focus=[fx, fy], freeze=false, rot=deg, whip_in=s, whip_out=s, wipe_in={src, from, dur}, note}
    freeze holds the frame at `from`; rot is a dutch tilt; zoom [z, z] is a static cut-in aimed at focus;
    whip_in / whip_out smear the frame sideways at the start / end; wipe_in reveals this shot over
    another clip (src at from) with a paint-drip mask.
    "end" anchors the source end instead of the start: from = end - dur*speed.
  overlays: [{type: "text_slam" | "starburst" | "flash", at, dur, text, color, stroke, size, font}
             | {type: "text_pop", at, dur, out, pieces: [{text, at, x, y, rot, style?}], style: {font, size,
                fill, fill2, stroke, stroke_w, stroke2, stroke2_w, shadow, glow, shear}, wobble: [t...], sparks,
                pop, pop_from (>1: slams in oversized and settles), exit: "whip"}]
             | {type: "layer", src: RGBA .mov (qtrle), at, dur}   e.g. a lyrics layer rendered elsewhere
    text_pop times (pieces[].at, wobble, out) are absolute song seconds: put each piece on the
    sung character (lyric_chars.py), not on the beat grid.

Fonts: a piece's style `font`, else $MV_REPLICATE_FONT, else HarmonyOS Sans SC Black if installed.
work_dir is deleted and recreated on every run: point it at a scratch folder.

  python render_edl.py edl.json [--root DIR]
"""
import argparse, json, math, os, shutil, subprocess, sys
from pathlib import Path

ROOT = Path.cwd()


def default_font():
    """Body font: $MV_REPLICATE_FONT, else HarmonyOS Sans SC Black in a usual font folder."""
    env = os.environ.get('MV_REPLICATE_FONT')
    if env:
        return Path(env).expanduser()
    for d in ('~/Library/Fonts', '/Library/Fonts', '~/.local/share/fonts', '~/.fonts', '/usr/share/fonts/truetype'):
        p = Path(d).expanduser() / 'HarmonyOS_Sans_SC_Black.ttf'
        if p.exists():
            return p
    sys.exit('no default font: set MV_REPLICATE_FONT=/path/to/a/CJK/font.ttf or give "font" in the EDL style (see README)')


def P(p):
    p = Path(p).expanduser()
    return p if p.is_absolute() else ROOT / p


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"ffmpeg failed: {' '.join(map(str, cmd))}\n{r.stderr[-2000:]}")
    return r


def hexrgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def frames(t, fps):
    return round(t * fps)


def seg_filter(s, W, H, fps, grade):
    speed = s.get('speed', 1.0)
    f = [f'setpts=(PTS-STARTPTS)/{speed}', f'fps={fps}']
    if s.get('freeze'):             # hold the frame at `from` for the whole segment (freeze-frame card)
        f = ['trim=end_frame=1', 'setpts=PTS-STARTPTS', f"tpad=stop_mode=clone:stop_duration={s['dur'] + 0.2}", f'fps={fps}']
    if s.get('reverse'):            # play the selected source range backwards
        f.insert(0, 'reverse')
    lay = s.get('layout', 'full')
    if lay == 'full':
        f += [f'scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos', f'crop={W}:{H}']
    if s.get('rot') and lay == 'full':   # dutch tilt, scaled up so no corner shows
        a = abs(s['rot']) * math.pi / 180
        k = math.cos(a) + max(W / H, H / W) * math.sin(a)
        f += [f'scale={int(W * k + 2) // 2 * 2}:-2', f"rotate={s['rot']}*PI/180", f'crop={W}:{H}']
    if grade:
        f.append(grade)
    if s.get('duotone'):            # single-hue print: grey levels mapped through 2 or 3 colour stops
        stops = [hexrgb(c) for c in s['duotone']]          # [shadow, (mid,) highlight]
        def lut(ch):
            if len(stops) == 2:
                a, b = stops[0][ch], stops[1][ch]
                return f"{a}+val*{(b - a) / 255:.4f}"
            a, m, b = stops[0][ch], stops[1][ch], stops[2][ch]
            return f"if(lt(val\\,128)\\,{a}+val*{(m - a) / 128:.4f}\\,{m}+(val-128)*{(b - m) / 127:.4f})"
        f += ['format=gray', 'format=rgb24', f"lutrgb=r='{lut(0)}':g='{lut(1)}':b='{lut(2)}'"]
    if s.get('zoom') and lay == 'full':   # slow push / pull over the whole segment, e.g. [1.0, 1.12]
        z0, z1 = s['zoom']
        fx, fy = s.get('focus', [0.5, 0.5])          # where the push aims, as a fraction of the frame
        if z0 == z1:                                # a static cut-in on the same set-up
            f += [f'scale={int(W * z0) // 2 * 2}:-2:flags=bicubic', f'crop={W}:{H}:x=(iw-{W})*{fx}:y=(ih-{H})*{fy}']
        else:
            f += [f"scale=w='trunc({W}*({z0}+({z1 - z0})*t/{s['dur']})/2)*2':h=-2:eval=frame:flags=bicubic",
                  f'crop={W}:{H}:x=(iw-{W})*{fx}:y=(ih-{H})*{fy}']
    if s.get('punch'):
        p, d = 0.15, s['punch']
        f += [f"scale=w='trunc({W}*(1+{p}*max(0\\,1-t/{d}))/2)*2':h=-2:eval=frame", f'crop={W}:{H}']
    if s.get('shake'):
        d, aw, ah = s['shake'], W * 0.02, H * 0.02
        f += [f'scale={int(W * 1.05) // 2 * 2}:{int(H * 1.05) // 2 * 2}',
              f"crop={W}:{H}:x='(iw-{W})/2+if(lt(t,{d}),{aw:.1f}*sin(t*83)*(1-t/{d}),0)'"
              f":y='(ih-{H})/2+if(lt(t,{d}),{ah:.1f}*cos(t*71)*(1-t/{d}),0)'"]
    chain = ','.join(f)
    if lay == 'full' and (s.get('whip_in') or s.get('whip_out')):
        # whip pan across a cut: the frame slides sideways with a heavy horizontal smear; the gap it
        # leaves is filled with a blurred copy. whip_out on the shot before a cut, whip_in after it.
        if s.get('whip_in'):
            d = s['whip_in']
            chain += (f",split[wi1][wi2];[wi1]gblur=sigma=40[wib];[wib][wi2]overlay="
                      f"x='{W * 0.55:.0f}*pow(1-min(t/{d}\\,1)\\,2)':y=0,"
                      f"gblur=sigma=50:sigmaV=0.01:enable='lt(t,{d * 0.55:.3f})',"
                      f"gblur=sigma=14:sigmaV=0.01:enable='between(t,{d * 0.55:.3f},{d:.3f})'")
        if s.get('whip_out'):
            d, e = s['whip_out'], s['dur']
            chain += (f",split[wo1][wo2];[wo1]gblur=sigma=40[wob];[wob][wo2]overlay="
                      f"x='-{W * 0.55:.0f}*pow(max(0\\,(t-{e - d:.3f})/{d})\\,2)':y=0,"
                      f"gblur=sigma=14:sigmaV=0.01:enable='between(t,{e - d:.3f},{e - d * 0.45:.3f})',"
                      f"gblur=sigma=50:sigmaV=0.01:enable='gt(t,{e - d * 0.45:.3f})'")
    if isinstance(lay, dict) and lay.get('frame') == 'vertical':
        fh = int(H * 0.86) // 2 * 2
        fw = int(fh * 9 / 16) // 2 * 2
        b = max(4, W // 160)
        cx = {'left': W * 0.30, 'center': W * 0.5, 'right': W * 0.70}[lay.get('pos', 'center')]
        x0 = int(cx - (fw + 2 * b) / 2)
        y0 = (H - fh - 2 * b) // 2
        # slides in from the side; 'slide': false keeps it still (a freeze right after the same frame)
        slide = 0 if lay.get('slide') is False else W * 0.12 * (1 if lay.get('pos') == 'left' else -1)
        chain = (f"color=c={lay.get('bg', '#DCEBFF')}:s={W}x{H}:r={fps}[bg];"
                 f"[0:v]{chain},scale={fw}:{fh}:force_original_aspect_ratio=increase,crop={fw}:{fh},"
                 f"pad={fw + 2 * b}:{fh + 2 * b}:{b}:{b}:white[fr];"
                 f"[bg][fr]overlay=x='{x0}+{slide:.0f}*pow(1-min(t/0.22\\,1)\\,3)':y={y0}:shortest=1")
    elif isinstance(lay, dict) and lay.get('frame') == 'photo':
        # a print on a paper ground (photo-album ending): white border, tilted, slides up a little
        sc, ang = lay.get('scale', 0.8), lay.get('rot', -3)
        pw, ph = int(W * sc) // 2 * 2, int(H * sc) // 2 * 2
        b = max(6, W // 90)
        chain = (f"color=c={lay.get('bg', '#F4EEE2')}:s={W}x{H}:r={fps}[bg];"
                 f"[0:v]{chain},scale={pw}:{ph}:force_original_aspect_ratio=increase,crop={pw}:{ph},"
                 f"pad={pw + 2 * b}:{ph + 2 * b}:{b}:{b}:white,format=rgba,"
                 f"rotate={ang}*PI/180:c=none:ow=rotw({ang}*PI/180):oh=roth({ang}*PI/180)[fr];"
                 f"[bg][fr]overlay=x='(W-w)/2':y='(H-h)/2+{H * 0.06:.0f}*pow(1-min(t/0.35\\,1)\\,3)':shortest=1,format=yuv420p")
    else:
        chain = f'[0:v]{chain}'
    if s.get('flash_in'):
        chain += f",fade=t=in:st=0:d={s['flash_in']}:color=white"
    return chain


def drip_mask(W, H, fps, n, d, out, seed=0):
    """White-on-black frames: paint runs down from the top in uneven drips until the frame is
    covered at `d` seconds; white afterwards."""
    import random
    from PIL import Image, ImageDraw
    out.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(seed)
    cols = 14
    w = W / cols
    drips = [(k * w + w / 2, rnd.uniform(0.0, 0.35), rnd.uniform(0.55, 1.0), rnd.uniform(0.7, 1.25)) for k in range(cols)]
    for k in range(n):
        t = k / fps
        im = Image.new('L', (W, H), 0)
        if t >= d:
            im.paste(255, (0, 0, W, H))
        else:
            dr = ImageDraw.Draw(im)
            for cx, delay, speed, width in drips:
                p = min(1.0, max(0.0, (t / d - delay) / (1 - delay)) * (0.8 + 0.4 * speed))
                y = p * p * (H * 1.25)
                r = w * (0.62 + 0.25 * width)      # wider than the column pitch: no slits
                dr.rectangle([cx - r, -10, cx + r, y], fill=255)
                dr.ellipse([cx - r, y - r, cx + r, y + r], fill=255)          # rounded drip head
                if p > 0.15:                                                 # a small drop running ahead
                    rr = r * 0.35
                    dr.ellipse([cx - rr, y + r * 0.6, cx + rr, y + r * 0.6 + 2 * rr], fill=255)
        im.save(out / f'{k:05d}.png')
    return out


def render_segment(s, i, edl, work):
    W, H = edl['size']
    fps = edl['fps']
    n = frames(s['at'] + s['dur'], fps) - frames(s['at'], fps)
    out = work / f'seg{i:03d}.mp4'
    enc = ['-frames:v', str(n), '-an', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '16', '-pix_fmt', 'yuv420p', str(out)]
    if 'color' in s:
        run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f"color=c={s['color']}:s={W}x{H}:r={fps}", *enc])
        return out, n
    speed = s.get('speed', 1.0)
    start = s['from'] if 'from' in s else s['end'] - s['dur'] * speed
    need = s['dur'] * speed + 0.1
    if s.get('freeze'):
        need = 0.2
    chain = seg_filter(s, W, H, fps, edl.get('grade'))
    if s.get('wipe_in'):
        # paint-drip wipe: this shot is revealed over the previous one by a procedurally drawn mask
        wp = s['wipe_in']
        mask = drip_mask(W, H, fps, n, wp.get('dur', 0.9), work / f'mask{i:03d}', seed=i)
        a_chain = (f"setpts=PTS-STARTPTS,fps={fps},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
                   + (f",{edl['grade']}" if edl.get('grade') else '') + f",tpad=stop_mode=clone:stop_duration={s['dur']}")
        graph = (f"{chain}[b];[1:v]{a_chain}[a];[2:v]format=gray[m];[b][m]alphamerge[bm];"
                 f"[a][bm]overlay=shortest=1,format=yuv420p")
        run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{max(start, 0):.4f}', '-t', f'{need:.4f}', '-i', str(P(s['src'])),
             '-ss', f"{wp['from']:.4f}", '-t', f"{wp.get('dur', 0.9) + 0.1:.4f}", '-i', str(P(wp['src'])),
             '-framerate', str(fps), '-i', str(mask / '%05d.png'),
             '-filter_complex', graph + '[v]', '-map', '[v]', *enc])
        shutil.rmtree(mask)
    else:
        run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{max(start, 0):.4f}', '-t', f'{need:.4f}', '-i', str(P(s['src'])),
             '-filter_complex', chain + '[v]', '-map', '[v]', *enc])
    got = int(run(['ffprobe', '-v', 'error', '-count_frames', '-select_streams', 'v:0', '-show_entries',
                   'stream=nb_read_frames', '-of', 'csv=p=0', str(out)]).stdout.strip())
    if got < n:
        sys.exit(f"segment {i} ({s.get('note', '')}) is short: {got}/{n} frames; source runs out at {start + need:.2f}s")
    return out, n


def ease_out(x):
    return 1 - (1 - min(max(x, 0), 1)) ** 3


def paste_rgba(dst, src, x, y):
    """alpha_composite that tolerates sources hanging off any edge."""
    l, t = max(0, -x), max(0, -y)
    r, b = min(src.width, dst.width - x), min(src.height, dst.height - y)
    if r > l and b > t:
        dst.alpha_composite(src.crop((l, t, r, b)), (x + l, y + t))


def ease_back(x, k=2.2):
    x = min(max(x, 0), 1)
    return 1 + (k + 1) * (x - 1) ** 3 + k * (x - 1) ** 2


def text_sprite(text, st, H):
    """One styled piece of text as an RGBA image: gradient fill, inner and outer stroke,
    soft shadow, italic shear. Drawn once, transformed per frame."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    font = ImageFont.truetype(str(P(st['font']) if st.get('font') else default_font()), int(H * st['size']))
    s_in, s_out = int(H * st.get('stroke_w', 0.010)), int(H * st.get('stroke2_w', 0.008))
    pad = s_in + s_out + int(H * 0.04)
    l, t, r, b = font.getbbox(text, stroke_width=s_in + s_out)
    w, h = r - l + 2 * pad, b - t + 2 * pad
    org = (pad - l, pad - t)

    def mask(sw):
        m = Image.new('L', (w, h), 0)
        ImageDraw.Draw(m).text(org, text, font=font, fill=255, stroke_width=sw, stroke_fill=255)
        return m
    m_out, m_in, m_fill = mask(s_in + s_out), mask(s_in), mask(0)
    im = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    if st.get('marker'):            # highlighter stroke behind the lower half of the text (credit cards)
        gl, gt, gr, gb = font.getbbox(text)
        x0, x1 = org[0] + gl - s_in - H * 0.02, org[0] + gr + s_in + H * 0.03     # clears the white rim
        y0, y1 = org[1] + gt + (gb - gt) * 0.42, org[1] + gb + s_in + H * 0.014
        sk = (y1 - y0) * 0.25          # the pen goes on at a slight slant
        ImageDraw.Draw(im).polygon([(x0 + sk, y0), (x1 + sk, y0 - sk * 0.4), (x1, y1 - sk * 0.4), (x0, y1)],
                                   fill=hexrgb(st['marker']) + (int(255 * st.get('marker_a', 0.85)),))
    if st.get('shadow'):
        sh = Image.new('RGBA', (w, h), hexrgb(st['shadow']) + (0,))
        sh.putalpha(m_out.point(lambda v: int(v * st.get('shadow_a', 0.55))))
        sh = sh.filter(ImageFilter.GaussianBlur(H * 0.008))
        dx, dy = int(H * 0.008), int(H * 0.014)
        paste_rgba(im, sh, dx, dy)
    if st.get('glow'):
        gl = Image.new('RGBA', (w, h), hexrgb(st['glow']) + (0,))
        gl.putalpha(m_out.filter(ImageFilter.GaussianBlur(H * 0.018)).point(lambda v: min(255, int(v * 1.4))))
        im.alpha_composite(gl)
    if s_out:
        im.alpha_composite(Image.composite(Image.new('RGBA', (w, h), hexrgb(st.get('stroke2', '#FFFFFF')) + (255,)),
                                           Image.new('RGBA', (w, h), (0, 0, 0, 0)), m_out))
    im.alpha_composite(Image.composite(Image.new('RGBA', (w, h), hexrgb(st.get('stroke', '#1E3A8A')) + (255,)),
                                       Image.new('RGBA', (w, h), (0, 0, 0, 0)), m_in))
    c1, c2 = hexrgb(st.get('fill', '#FFFFFF')), hexrgb(st.get('fill2', st.get('fill', '#FFFFFF')))
    grad = Image.new('RGBA', (1, h))
    for y in range(h):
        f = min(max((y - pad) / max(1, h - 2 * pad), 0), 1)
        grad.putpixel((0, y), tuple(int(c1[i] + (c2[i] - c1[i]) * f) for i in range(3)) + (255,))
    im.alpha_composite(Image.composite(grad.resize((w, h)), Image.new('RGBA', (w, h), (0, 0, 0, 0)), m_fill))
    if st.get('shear'):
        sx = st['shear']
        im = im.transform((w + int(abs(sx) * h), h), Image.AFFINE, (1, sx, -abs(sx) * h if sx > 0 else 0, 0, 1, 0),
                          Image.BICUBIC)
    return im


def text_pop_frame(o, t_abs, W, H, sprites):
    """Pieces of text that pop in on given times (lyric characters), wobble on given
    times, and leave together. Everything in absolute song time."""
    from PIL import Image, ImageDraw
    im = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    lead = o.get('lead', 0.04)
    out_t, out_d = o['out'], o.get('out_dur', 0.16)
    for pc, spr in zip(o['pieces'], sprites):
        x = t_abs - (pc['at'] - lead)
        if x < 0:
            continue
        pop = o.get('pop', 0.2)
        if o.get('pop_from', 0) > 1:      # slams in oversized and settles (title logo)
            sc = 1 + (o['pop_from'] - 1) * (1 - ease_out(x / pop)) if x < pop else 1.0
        else:
            sc = 0.2 + 0.8 * ease_back(x / pop) if x < pop else 1.0
        rot = pc.get('rot', 0) + o.get('pop_spin', 18) * (1 - ease_out(x / pop))
        for w in o.get('wobble', []):
            y = t_abs - w
            if 0 <= y < 0.45:
                env = math.exp(-y / 0.11)
                rot += o.get('wobble_deg', 7) * math.sin(2 * math.pi * 16 * y) * env
                sc *= 1 + 0.07 * env
        a, dx = 1.0, 0
        if t_abs > out_t:
            z = (t_abs - out_t) / out_d
            if o.get('exit') == 'whip':   # leaves with the whip pan of the shot under it
                a, dx = max(0.0, 1 - 0.6 * z), -W * 0.55 * min(z, 1) ** 2
            else:
                a, sc = max(0.0, 1 - z), sc * (1 + 0.15 * z)
        if a <= 0 or sc <= 0.01:
            continue
        s = spr.resize((max(1, int(spr.width * sc)), max(1, int(spr.height * sc))), Image.BICUBIC)
        s = s.rotate(-rot, resample=Image.BICUBIC, expand=True)
        if a < 1:
            s.putalpha(s.getchannel('A').point(lambda v: int(v * a)))
        paste_rgba(im, s, int(pc['x'] * W - s.width / 2 + dx), int(pc['y'] * H - s.height / 2))
        if o.get('sparks', True) and 0 <= x < 0.3:     # a ring of short white sparks on each pop
            dr = ImageDraw.Draw(im)
            p = ease_out(x / 0.3)
            cx, cy = pc['x'] * W, pc['y'] * H
            r0, r1 = H * (0.10 + 0.12 * p), H * (0.13 + 0.16 * p)
            for k in range(8):
                ang = k * math.pi / 4 + 0.3
                dr.line([(cx + r0 * math.cos(ang), cy + r0 * math.sin(ang)),
                         (cx + r1 * math.cos(ang), cy + r1 * math.sin(ang))],
                        fill=(255, 255, 255, int(235 * (1 - p))), width=max(2, int(H * 0.006)))
    return im


def overlay_frames(o, W, H, fps, d):
    from PIL import Image, ImageDraw, ImageFont
    n = frames(o['dur'], fps)
    d.mkdir(parents=True, exist_ok=True)
    font = None
    if o['type'] == 'text_pop':
        sprites = [text_sprite(pc['text'], {**o['style'], **pc.get('style', {})}, H) for pc in o['pieces']]
        for k in range(n):
            text_pop_frame(o, o['at'] + k / fps, W, H, sprites).save(d / f'{k:05d}.png')
        return n
    if o['type'] == 'text_slam':
        font = ImageFont.truetype(str(P(o['font']) if o.get('font') else default_font()), int(H * o.get('size', 0.42)))
    for k in range(n):
        t = k / fps
        im = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        dr = ImageDraw.Draw(im)
        if o['type'] == 'text_slam':
            sc = 1 + 0.9 * (1 - ease_out(t / 0.16))          # slams in from 1.9x in 0.16 s
            a = 1.0 if t < o['dur'] - 0.12 else max(0, (o['dur'] - t) / 0.12)
            layer = Image.new('RGBA', (W, H), (0, 0, 0, 0))
            ld = ImageDraw.Draw(layer)
            sw = max(4, int(H * 0.012))
            ld.text((W / 2, H / 2), o['text'], font=font, fill=hexrgb(o.get('color', '#FFFFFF')) + (255,),
                    anchor='mm', stroke_width=sw, stroke_fill=hexrgb(o.get('stroke', '#1E3A8A')) + (255,))
            if sc != 1:
                layer = layer.resize((int(W * sc), int(H * sc)), Image.LANCZOS)
                l, tp = (layer.width - W) // 2, (layer.height - H) // 2
                layer = layer.crop((l, tp, l + W, tp + H))
            if a < 1:
                layer.putalpha(layer.getchannel('A').point(lambda v: int(v * a)))
            im = layer
            if t < 0.1:                                      # white hit on impact
                im = Image.alpha_composite(Image.new('RGBA', (W, H), (255, 255, 255, int(200 * (1 - t / 0.1)))), im)
        elif o['type'] == 'starburst':
            p = ease_out(t / (o['dur'] * 0.6))
            fade = max(0.0, 1 - t / o['dur'])
            cx, cy = W / 2, H / 2
            for r in range(24):
                ang = r * 2 * math.pi / 24 + 0.13 * (r % 3)
                ln = (0.25 + 0.75 * p) * W * (0.6 + 0.4 * ((r * 7) % 5) / 4)
                wdt = max(2, int(H * 0.01 * (1 + (r % 4))))
                dr.line([(cx, cy), (cx + ln * math.cos(ang), cy + ln * math.sin(ang))],
                        fill=(255, 255, 255, int(230 * fade)), width=wdt)
            core = int(H * (0.05 + 0.5 * p))
            dr.ellipse([cx - core, cy - core, cx + core, cy + core], fill=(255, 255, 255, int(255 * fade)))
            if t < 0.12:
                im = Image.alpha_composite(im, Image.new('RGBA', (W, H), (255, 255, 255, int(220 * (1 - t / 0.12)))))
        elif o['type'] == 'flash':
            im = Image.new('RGBA', (W, H), (255, 255, 255, int(255 * max(0, 1 - t / o['dur']))))
        im.save(d / f'{k:05d}.png')
    return n


def main():
    global ROOT
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('edl', help='edit decision list (JSON)')
    ap.add_argument('--root', default='.', help='folder that relative paths in the EDL resolve against')
    a = ap.parse_args()
    ROOT = Path(a.root).expanduser().resolve()
    edl_path = Path(a.edl)
    edl = json.loads(edl_path.read_text())
    W, H = edl['size']
    fps, dur = edl['fps'], edl['duration']
    work = P(edl['work_dir'])
    if work in (ROOT, Path.home(), Path('/')) or work in ROOT.parents or edl_path.resolve().is_relative_to(work):
        sys.exit(f'work_dir {work} would delete the root, your home or the EDL itself; use a scratch folder')
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    segs = edl['segments']
    t = 0.0
    for i, s in enumerate(segs):
        if abs(s['at'] - t) > 1e-3:
            sys.exit(f'segment {i} starts at {s["at"]}, expected {t:.3f} (segments must be contiguous)')
        t = s['at'] + s['dur']
    if abs(t - dur) > 1e-3:
        sys.exit(f'segments end at {t:.3f}, duration is {dur}')

    parts = []
    for i, s in enumerate(segs):
        parts.append(render_segment(s, i, edl, work)[0])
        print(f"seg {i:2d} {s['at']:6.2f}+{s['dur']:.2f} {s.get('note', '')}", flush=True)
    (work / 'concat.txt').write_text(''.join(f"file '{p.name}'\n" for p in parts))
    base = work / 'base.mp4'
    run(['ffmpeg', '-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', str(work / 'concat.txt'), '-c', 'copy', str(base)])

    inputs, fc, last = ['-i', str(base)], [], '[0:v]'
    for j, o in enumerate(edl.get('overlays', [])):
        if o['type'] == 'layer':      # a ready-made RGBA layer (e.g. lyrics), placed at `at`
            mov = P(o['src'])
        else:
            d = work / f'ov{j}'
            overlay_frames(o, W, H, fps, d)
            mov = work / f'ov{j}.mov'
            run(['ffmpeg', '-v', 'error', '-y', '-framerate', str(fps), '-i', str(d / '%05d.png'), '-c:v', 'qtrle', str(mov)])
            shutil.rmtree(d)
        inputs += ['-itsoffset', f"{o['at']:.4f}", '-i', str(mov)]
        fc.append(f"{last}[{j + 1}:v]overlay=eof_action=pass:enable='between(t,{o['at']:.4f},{o['at'] + o['dur']:.4f})'[o{j}]")
        last = f'[o{j}]'
    ef = edl.get('end_fade', 0)
    fc.append(f"{last}fade=t=out:st={dur - ef:.3f}:d={ef}[v]" if ef else f'{last}null[v]')

    # Audio is rendered to its own WAV first and muxed afterwards. Do not put loudnorm inside the
    # video filter graph: in dynamic mode it once left a 3 s hole in the AAC timestamps (0.68–3.67 s,
    # the first sung line went silent). Loudness is a flat gain plus a
    # peak limiter, not loudnorm's dynamic mode, which lifted the start ~3 dB more than the end.
    song = P(edl['song'])
    af = f'atrim=0:{dur},afade=t=out:st={dur - max(ef, 0.5):.3f}:d={max(ef, 0.5)}'
    if edl.get('loudness'):
        L = edl['loudness']
        r = subprocess.run(['ffmpeg', '-hide_banner', '-i', str(song), '-af',
                            f"{af},loudnorm=I={L['I']}:TP={L['TP']}:LRA=11:print_format=json", '-f', 'null', '-'],
                           capture_output=True, text=True).stderr
        m = json.loads(r[r.rfind('{'):r.rfind('}') + 1])
        gain = L['I'] - float(m['input_i'])
        limit = 10 ** ((L['TP'] - 0.5) / 20)   # sample-peak ceiling with headroom for inter-sample peaks
        if float(m['input_tp']) + gain > L['TP']:
            print(f"loudness: +{gain:.2f} dB puts peaks over {L['TP']} dBTP; a limiter shaves about "
                  f"{float(m['input_tp']) + gain - L['TP'] + 0.5:.1f} dB off the loudest peaks; say so in the report", flush=True)
        af += f',volume={gain:.2f}dB,alimiter=limit={limit:.4f}:attack=5:release=50:level=false'
    wav = work / 'audio.wav'
    run(['ffmpeg', '-v', 'error', '-y', '-i', str(song), '-af', f'{af},aresample=48000', '-c:a', 'pcm_s24le', str(wav)])
    out = P(edl['out'])
    out.parent.mkdir(parents=True, exist_ok=True)
    nidx = len(edl.get('overlays', [])) + 1
    run(['ffmpeg', '-v', 'error', '-y', *inputs, '-i', str(wav), '-filter_complex', ';'.join(fc),
         '-map', '[v]', '-map', f'{nidx}:a', '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p',
         '-r', str(fps), '-frames:v', str(frames(dur, fps)), '-c:a', 'aac', '-b:a', '256k', '-movflags', '+faststart', str(out)])
    print('wrote', out)


if __name__ == '__main__':
    main()
