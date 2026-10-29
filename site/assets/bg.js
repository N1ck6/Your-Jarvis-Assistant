/* The living background: a WebGL neon tunnel (grid floor and ceiling, reactor rings, aurora, cursor light)
   and a 2D layer of particles linked like a hologram. Scroll flies the camera forward and tilts it;
   window.JarvisBG lets the page set the hue (0 cyan, 1 amber) and the scroll velocity. */
(() => {
  "use strict";
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const mobile = matchMedia("(max-width: 767px)").matches;
  const state = { hue: 0, hueTarget: 0, vel: 0, mouse: [0.5, 0.45], mouseTarget: [0.5, 0.45], scroll: 0 };
  window.JarvisBG = {
    setHue(h) { state.hueTarget = h; },
    setVelocity(v) { state.vel = v; },
  };
  addEventListener("pointermove", (e) => { state.mouseTarget = [e.clientX / innerWidth, 1 - e.clientY / innerHeight]; }, { passive: true });

  /* ---------------------------------------------------------------- WebGL tunnel */
  const gl_canvas = document.getElementById("bg-gl");
  const gl = gl_canvas.getContext("webgl", { antialias: false, alpha: false, powerPreference: "low-power" });
  const VERT = "attribute vec2 p;void main(){gl_Position=vec4(p,0.,1.);}";
  const FRAG = `
precision mediump float;
uniform vec2 uRes; uniform float uTime; uniform vec2 uMouse; uniform float uScroll; uniform float uPage;
uniform float uHue; uniform float uVel;

float hash(vec2 p){ p = fract(p * vec2(123.34, 456.21)); p += dot(p, p + 45.32); return fract(p.x * p.y); }
float noise(vec2 p){ vec2 i = floor(p), f = fract(p); vec2 u = f * f * (3. - 2. * f);
  return mix(mix(hash(i), hash(i + vec2(1., 0.)), u.x), mix(hash(i + vec2(0., 1.)), hash(i + vec2(1., 1.)), u.x), u.y); }
float fbm(vec2 p){ float v = 0., a = .5; for (int i = 0; i < 4; i++) { v += a * noise(p); p *= 2.03; a *= .5; } return v; }
float line(float x, float w){ float d = abs(fract(x) - .5); return smoothstep(.5 - w, .5, d); }
float ring(float r, float r0, float w){ return smoothstep(w, 0., abs(r - r0)); }

void main(){
  vec2 uv = (gl_FragCoord.xy - .5 * uRes) / uRes.y;
  float t = uTime;
  vec3 cyan = vec3(.16, .66, .88), glow = vec3(.56, .86, 1.), amber = vec3(.96, .74, .26);
  vec3 main = mix(cyan, amber, uHue), light = mix(glow, vec3(1., .88, .6), uHue);
  vec3 col = vec3(.006, .014, .026);

  // aurora clouds drifting with time and scroll
  float n = fbm(uv * 1.4 + vec2(t * .025, -t * .018) + vec2(0., uScroll * .00025));
  float n2 = fbm(uv * 2.6 - vec2(t * .02, uScroll * .0004));
  col += main * smoothstep(.48, .95, n) * .16;
  col += vec3(.1, .2, .55) * smoothstep(.55, 1., n2) * .10;

  // the camera tilts over the page and flies forward with the scroll
  float horizon = mix(-.3, .04, uPage) + sin(t * .2) * .01;
  float travel = t * .35 + uScroll * .0022;
  float speed = clamp(abs(uVel) * .02, 0., 1.);

  // floor
  float dy = horizon - uv.y;
  if (dy > 0.) {
    float z = .38 / dy;
    vec2 g = vec2(uv.x * z * 1.1, z + travel);
    float w = clamp(.018 * z, .02, .45);
    float l = max(line(g.x, w), line(g.y, w));
    float fog = exp(-z * .085);
    col += main * l * fog * (.55 + speed * .8);
    // pulses running along the lanes towards the viewer
    float lane = floor(g.x);
    float pulse = smoothstep(.96, 1., fract(g.y * .12 - t * .5 + hash(vec2(lane, 3.))));
    col += light * pulse * line(g.x, w * 1.6) * fog * 1.2;
  }
  // ceiling, fainter: the tunnel
  float cy = uv.y - (horizon + .66);
  if (cy > 0.) {
    float z = .5 / cy;
    vec2 g = vec2(uv.x * z * 1.1, z + travel * .8);
    float w = clamp(.018 * z, .02, .45);
    col += main * max(line(g.x, w), line(g.y, w)) * exp(-z * .11) * .22;
  }
  // neon horizon
  col += light * exp(-abs(uv.y - horizon) * 60.) * .55;
  col += main * exp(-abs(uv.y - horizon) * 9.) * .12;

  // reactor rings behind the hero, rising slower than the page
  vec2 c = vec2(0., .1 + uScroll / uRes.y * .55);
  vec2 q = uv - c;
  float r = length(q);
  float a = atan(q.y, q.x) / 6.28318 + .5;
  float hero = clamp(1. - uScroll / uRes.y * .9, 0., 1.);
  float rings = 0.;
  rings += ring(r, .30, .002) * .8;
  rings += ring(r, .355, .012) * step(fract(a * 72. + t * .02), .62) * .7;
  rings += ring(r, .43, .0025) * step(fract(a * 3. - t * .05), .28);
  rings += ring(r, .50, .0018) * .5;
  rings += ring(r, .535, .01) * step(fract(a * 180.), .18) * .6;
  rings += ring(r, .64, .002) * step(fract(a * 2. + t * .03), .4) * .6;
  col += light * rings * hero * .55;
  col += main * exp(-r * 4.) * hero * .18;

  // light under the cursor shows a dot grid
  vec2 m = (uMouse - .5) * vec2(uRes.x / uRes.y, 1.);
  float spot = exp(-length(uv - m) * 3.2);
  vec2 cell = fract(uv * 28.) - .5;
  float dots = smoothstep(.08, .0, length(cell));
  col += light * dots * spot * .35 + main * spot * .06;

  // vignette and grain
  col *= smoothstep(1.35, .25, length(uv * vec2(.75, 1.)));
  col += (hash(gl_FragCoord.xy + fract(t) * 91.) - .5) * .018;
  gl_FragColor = vec4(col, 1.);
}`;

  let prog = null, uni = {};
  if (gl) {
    const sh = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; };
    prog = gl.createProgram();
    gl.attachShader(prog, sh(gl.VERTEX_SHADER, VERT));
    gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, FRAG));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) { prog = null; document.documentElement.classList.add("no-gl"); }
    else {
      gl.useProgram(prog);
      const buf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
      const loc = gl.getAttribLocation(prog, "p");
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
      ["uRes", "uTime", "uMouse", "uScroll", "uPage", "uHue", "uVel"].forEach((n) => { uni[n] = gl.getUniformLocation(prog, n); });
    }
  } else document.documentElement.classList.add("no-gl");

  /* ---------------------------------------------------------------- particles */
  const fx = document.getElementById("bg-fx");
  const g2 = fx.getContext("2d");
  const COUNT = mobile ? 34 : 80;
  const pts = Array.from({ length: COUNT }, () => ({
    x: Math.random() * 2 - 1, y: Math.random() * 2 - 1, z: Math.random() * 0.9 + 0.1,
    vx: (Math.random() - 0.5) * 0.0006, vy: (Math.random() - 0.5) * 0.0006,
  }));

  function size() {
    const scale = mobile ? 0.45 : (devicePixelRatio > 1.5 ? 0.5 : 0.65);
    gl_canvas.width = Math.round(innerWidth * scale);
    gl_canvas.height = Math.round(innerHeight * scale);
    if (gl && prog) gl.viewport(0, 0, gl_canvas.width, gl_canvas.height);
    const dpr = Math.min(devicePixelRatio || 1, 2);
    fx.width = innerWidth * dpr; fx.height = innerHeight * dpr;
    g2.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  size();
  addEventListener("resize", size);

  let running = !document.hidden;
  document.addEventListener("visibilitychange", () => { running = !document.hidden; if (running) requestAnimationFrame(frame); });

  const start = performance.now();
  function frame(now) {
    if (!running) return;
    if (!reduced) requestAnimationFrame(frame);
    const t = (now - start) / 1000;
    state.hue += (state.hueTarget - state.hue) * 0.04;
    state.mouse[0] += (state.mouseTarget[0] - state.mouse[0]) * 0.06;
    state.mouse[1] += (state.mouseTarget[1] - state.mouse[1]) * 0.06;
    state.scroll += (scrollY - state.scroll) * 0.12;
    const max = Math.max(1, document.documentElement.scrollHeight - innerHeight);

    if (gl && prog) {
      gl.uniform2f(uni.uRes, gl_canvas.width, gl_canvas.height);
      gl.uniform1f(uni.uTime, t);
      gl.uniform2f(uni.uMouse, state.mouse[0], state.mouse[1]);
      gl.uniform1f(uni.uScroll, state.scroll * (gl_canvas.height / innerHeight));
      gl.uniform1f(uni.uPage, Math.min(1, state.scroll / max));
      gl.uniform1f(uni.uHue, state.hue);
      gl.uniform1f(uni.uVel, state.vel);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }

    // particles: depth parallax with the cursor and the scroll
    const w = innerWidth, h = innerHeight;
    g2.clearRect(0, 0, w, h);
    const mx = (state.mouse[0] - 0.5), my = (0.5 - state.mouse[1]);
    const hueMix = state.hue;
    const rgb = `${Math.round(143 + 112 * hueMix)},${Math.round(220 - 20 * hueMix)},${Math.round(255 - 150 * hueMix)}`;
    const proj = [];
    for (const p of pts) {
      if (!reduced) { p.x += p.vx; p.y += p.vy; }
      if (p.x < -1.1) p.x = 1.1; if (p.x > 1.1) p.x = -1.1;
      const sy = ((p.y - state.scroll * 0.0006 * p.z) % 2.2 + 3.3) % 2.2 - 1.1;
      const px = w / 2 + (p.x + mx * 0.18 * p.z) * w * 0.55;
      const py = h / 2 + (sy + my * 0.18 * p.z) * h * 0.55;
      proj.push([px, py, p.z]);
    }
    const linkDist = mobile ? 90 : 130;
    g2.lineWidth = 1;
    for (let i = 0; i < proj.length; i++) {
      const [ax, ay, az] = proj[i];
      for (let j = i + 1; j < proj.length; j++) {
        const dx = ax - proj[j][0], dy = ay - proj[j][1];
        const d = Math.hypot(dx, dy);
        if (d < linkDist) {
          g2.strokeStyle = `rgba(${rgb},${(1 - d / linkDist) * 0.22 * Math.min(az, proj[j][2])})`;
          g2.beginPath(); g2.moveTo(ax, ay); g2.lineTo(proj[j][0], proj[j][1]); g2.stroke();
        }
      }
      // the cursor scans the nearest points
      const cx = state.mouseTarget[0] * w, cyy = (1 - state.mouseTarget[1]) * h;
      const dc = Math.hypot(ax - cx, ay - cyy);
      if (!mobile && dc < 180) {
        g2.strokeStyle = `rgba(${rgb},${(1 - dc / 180) * 0.45})`;
        g2.beginPath(); g2.moveTo(ax, ay); g2.lineTo(cx, cyy); g2.stroke();
      }
      g2.fillStyle = `rgba(${rgb},${0.25 + az * 0.6})`;
      g2.beginPath(); g2.arc(ax, ay, 0.6 + az * 1.6, 0, Math.PI * 2); g2.fill();
    }
  }
  requestAnimationFrame(frame);
})();
