import * as THREE from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass }     from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass }from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass }     from 'three/addons/postprocessing/OutputPass.js';

// --------------------------------------------------------------------
// Shared uniforms — uVoiceBright is the public driver (audio later).
// --------------------------------------------------------------------
const uTime        = { value: 0 };
const uVoiceBright = { value: 0 };     // 0.0 – 1.0
const uResolution  = { value: new THREE.Vector2(1, 1) };

// Public handle so external code (audio graph) can raise the orb.
window.CORA = {
  get voiceBright() { return uVoiceBright.value; },
  set voiceBright(v) {
    uVoiceBright.value = Math.max(0, Math.min(1, v));
  },
};

// --------------------------------------------------------------------
// Renderer / scene / camera
// --------------------------------------------------------------------
const canvas = document.getElementById('scene');
const renderer = new THREE.WebGLRenderer({
  canvas,
  antialias: true,
  alpha: false,
  powerPreference: 'high-performance',
});
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setClearColor(new THREE.Color(0x0E0F13), 1);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.1;

const scene  = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 200);
camera.position.set(0, 0, 6);

// --------------------------------------------------------------------
// Nebula background — full-screen quad with procedural FBM noise
//   layered orange / purple / blue wisps that slowly drift.
// --------------------------------------------------------------------
const nebulaMat = new THREE.ShaderMaterial({
  uniforms: { uTime, uResolution },
  depthTest: false,
  depthWrite: false,
  vertexShader: /* glsl */`
    void main() {
      gl_Position = vec4(position.xy, 0.999, 1.0); // push to far plane
    }
  `,
  fragmentShader: /* glsl */`
    precision highp float;
    uniform float uTime;
    uniform vec2  uResolution;

    // hash / noise / fbm — classic Inigo Quilez style
    float hash(vec2 p) {
      p = fract(p * vec2(123.34, 456.21));
      p += dot(p, p + 45.32);
      return fract(p.x * p.y);
    }
    float noise(vec2 p) {
      vec2 i = floor(p);
      vec2 f = fract(p);
      vec2 u = f * f * (3.0 - 2.0 * f);
      float a = hash(i);
      float b = hash(i + vec2(1.0, 0.0));
      float c = hash(i + vec2(0.0, 1.0));
      float d = hash(i + vec2(1.0, 1.0));
      return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
    }
    float fbm(vec2 p) {
      float v = 0.0;
      float a = 0.5;
      for (int i = 0; i < 5; i++) {
        v += a * noise(p);
        p *= 2.02;
        a *= 0.5;
      }
      return v;
    }

    void main() {
      vec2 uv = gl_FragCoord.xy / uResolution.xy;
      vec2 p  = (uv - 0.5) * vec2(uResolution.x / uResolution.y, 1.0);

      float t = uTime * 0.015;

      // Three drifting nebula layers, each in its own colour & direction.
      float n1 = fbm(p * 1.6 + vec2( t,         t * 0.4));
      float n2 = fbm(p * 2.3 + vec2(-t * 0.6,   t * 0.2) + 7.3);
      float n3 = fbm(p * 3.1 + vec2( t * 0.3,  -t * 0.7) + 19.1);

      vec3 base   = vec3(0.055, 0.059, 0.075);          // #0E0F13
      vec3 orange = vec3(0.984, 0.573, 0.235);          // #FB923C
      vec3 purple = vec3(0.50,  0.30,  0.85);
      vec3 blue   = vec3(0.20,  0.45,  0.95);

      // Distance-from-centre vignette so nebula fades toward edges
      float r = length(p);
      float vignette = smoothstep(1.4, 0.15, r);

      vec3 col = base;
      col += orange * pow(n1, 2.5) * 0.55 * vignette;
      col += purple * pow(n2, 3.0) * 0.45 * vignette;
      col += blue   * pow(n3, 3.5) * 0.40 * vignette;

      // subtle grain to kill banding
      float grain = (hash(gl_FragCoord.xy + uTime) - 0.5) * 0.015;
      col += grain;

      gl_FragColor = vec4(col, 1.0);
    }
  `,
});
const nebula = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), nebulaMat);
nebula.frustumCulled = false;
nebula.renderOrder   = -10;
scene.add(nebula);

// --------------------------------------------------------------------
// Starfield — THREE.Points with per-star twinkle
// --------------------------------------------------------------------
const STAR_COUNT = 900;
const starGeo = new THREE.BufferGeometry();
const starPos    = new Float32Array(STAR_COUNT * 3);
const starSeed   = new Float32Array(STAR_COUNT);
const starSize   = new Float32Array(STAR_COUNT);
for (let i = 0; i < STAR_COUNT; i++) {
  // distribute in a shell well behind the orb
  const r = 30 + Math.random() * 40;
  const theta = Math.random() * Math.PI * 2;
  const phi   = Math.acos(2 * Math.random() - 1);
  starPos[i*3+0] = r * Math.sin(phi) * Math.cos(theta);
  starPos[i*3+1] = r * Math.sin(phi) * Math.sin(theta);
  starPos[i*3+2] = r * Math.cos(phi) - 20; // bias behind
  starSeed[i] = Math.random() * 1000.0;
  starSize[i] = 0.6 + Math.random() * 2.2;
}
starGeo.setAttribute('position', new THREE.BufferAttribute(starPos, 3));
starGeo.setAttribute('aSeed',    new THREE.BufferAttribute(starSeed, 1));
starGeo.setAttribute('aSize',    new THREE.BufferAttribute(starSize, 1));

const starMat = new THREE.ShaderMaterial({
  uniforms: { uTime, uPixelRatio: { value: renderer.getPixelRatio() } },
  transparent: true,
  depthWrite: false,
  blending: THREE.AdditiveBlending,
  vertexShader: /* glsl */`
    attribute float aSeed;
    attribute float aSize;
    uniform float uTime;
    uniform float uPixelRatio;
    varying float vTwinkle;

    void main() {
      vec4 mv = modelViewMatrix * vec4(position, 1.0);
      // twinkle = sine with per-star offset & rate
      float rate = 0.6 + fract(aSeed * 0.013) * 2.2;
      float tw = sin(uTime * rate + aSeed) * 0.5 + 0.5;
      vTwinkle = mix(0.25, 1.0, tw);
      gl_Position = projectionMatrix * mv;
      gl_PointSize = aSize * uPixelRatio * (300.0 / -mv.z);
    }
  `,
  fragmentShader: /* glsl */`
    precision highp float;
    varying float vTwinkle;
    void main() {
      vec2 c = gl_PointCoord - 0.5;
      float d = length(c);
      float core = smoothstep(0.5, 0.0, d);
      float glow = smoothstep(0.5, 0.15, d) * 0.6;
      float a = (core + glow) * vTwinkle;
      if (a < 0.01) discard;
      // cool white with a faint warm cast
      vec3 col = mix(vec3(0.90, 0.95, 1.00), vec3(1.00, 0.90, 0.70), 0.25);
      gl_FragColor = vec4(col, a);
    }
  `,
});
const stars = new THREE.Points(starGeo, starMat);
scene.add(stars);

// --------------------------------------------------------------------
// Orb — bright core + three additive glow billboards
//   Layers (outer → inner):
//     1) wide soft atmospheric bloom
//     2) medium halo
//     3) bright inner core
// --------------------------------------------------------------------

function glowSprite({ radius, color, intensity, softness, voiceGain }) {
  const mat = new THREE.ShaderMaterial({
    uniforms: {
      uTime,
      uVoiceBright,
      uColor:     { value: new THREE.Color(color) },
      uIntensity: { value: intensity },
      uSoftness:  { value: softness },
      uVoiceGain: { value: voiceGain },
    },
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    vertexShader: /* glsl */`
      varying vec2 vUv;
      void main() {
        vUv = uv;
        // billboard: kill rotation from view matrix
        vec4 mv = modelViewMatrix * vec4(0.0, 0.0, 0.0, 1.0);
        mv.xy += position.xy;
        gl_Position = projectionMatrix * mv;
      }
    `,
    fragmentShader: /* glsl */`
      precision highp float;
      varying vec2 vUv;
      uniform vec3  uColor;
      uniform float uIntensity;
      uniform float uSoftness;
      uniform float uVoiceBright;
      uniform float uVoiceGain;
      uniform float uTime;

      void main() {
        float d = length(vUv - 0.5) * 2.0;   // 0 center → 1 edge
        float falloff = pow(clamp(1.0 - d, 0.0, 1.0), uSoftness);
        // gentle idle breath + voice drive
        float idle  = 0.5 + 0.5 * sin(uTime * (6.2831853 / 4.0));
        float breath = mix(0.85, 1.0, idle * 0.15);
        float vb    = uVoiceBright * uVoiceGain;
        float amp   = uIntensity * breath + vb;
        vec3 col = uColor * amp * falloff;
        float a  = falloff;
        gl_FragColor = vec4(col, a);
      }
    `,
  });
  const geo = new THREE.PlaneGeometry(radius * 2, radius * 2);
  const mesh = new THREE.Mesh(geo, mat);
  return mesh;
}

// 1) Wide atmospheric bloom — very soft, large, low intensity
const bloom = glowSprite({
  radius: 3.6,
  color: 0xFB923C,
  intensity: 0.35,
  softness: 2.8,
  voiceGain: 0.35,
});
bloom.renderOrder = 1;
scene.add(bloom);

// 2) Medium halo — mid size, mid intensity
const halo = glowSprite({
  radius: 1.9,
  color: 0xFDBA74,
  intensity: 0.85,
  softness: 2.0,
  voiceGain: 1.2,
});
halo.renderOrder = 2;
scene.add(halo);

// 3) Bright inner core glow — small, hot
const innerGlow = glowSprite({
  radius: 0.95,
  color: 0xFFE0BD,
  intensity: 1.35,
  softness: 1.6,
  voiceGain: 1.6,
});
innerGlow.renderOrder = 3;
scene.add(innerGlow);

// Solid core body — small emissive sphere so bloom pass has something hot to bite
const coreMat = new THREE.ShaderMaterial({
  uniforms: {
    uTime,
    uVoiceBright,
    uColor: { value: new THREE.Color(0xFB923C) },
  },
  transparent: false,
  vertexShader: /* glsl */`
    varying vec3 vNormal;
    varying vec3 vView;
    void main() {
      vec4 mv = modelViewMatrix * vec4(position, 1.0);
      vNormal = normalize(normalMatrix * normal);
      vView   = normalize(-mv.xyz);
      gl_Position = projectionMatrix * mv;
    }
  `,
  fragmentShader: /* glsl */`
    precision highp float;
    varying vec3 vNormal;
    varying vec3 vView;
    uniform vec3  uColor;
    uniform float uVoiceBright;
    uniform float uTime;

    void main() {
      float fres = pow(1.0 - max(dot(vNormal, vView), 0.0), 1.8);
      float idle = 0.5 + 0.5 * sin(uTime * (6.2831853 / 4.0));
      float body = 0.85 + idle * 0.08 + uVoiceBright * 0.6;
      vec3 col = uColor * body + vec3(1.0) * fres * (0.35 + uVoiceBright * 0.8);
      gl_FragColor = vec4(col, 1.0);
    }
  `,
});
const core = new THREE.Mesh(new THREE.SphereGeometry(0.42, 64, 64), coreMat);
core.renderOrder = 4;
scene.add(core);

// --------------------------------------------------------------------
// Post-processing: UnrealBloom on top of everything
// --------------------------------------------------------------------
const composer = new EffectComposer(renderer);
composer.addPass(new RenderPass(scene, camera));
const bloomPass = new UnrealBloomPass(
  new THREE.Vector2(1, 1),
  0.95,   // strength
  0.85,   // radius
  0.12    // threshold
);
composer.addPass(bloomPass);
composer.addPass(new OutputPass());

// --------------------------------------------------------------------
// Resize
// --------------------------------------------------------------------
function onResize() {
  const w = window.innerWidth;
  const h = window.innerHeight;
  renderer.setSize(w, h, false);
  composer.setSize(w, h);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  uResolution.value.set(w * renderer.getPixelRatio(), h * renderer.getPixelRatio());
}
window.addEventListener('resize', onResize);
onResize();

// --------------------------------------------------------------------
// Animation loop
// --------------------------------------------------------------------
const clock = new THREE.Clock();
function tick() {
  const dt = clock.getDelta();
  uTime.value += dt;

  // Gentle idle breathing floor so the orb is alive when quiet.
  // uVoiceBright (0..1) is set externally; we only read here for
  // parallax/ambient effects (actual visual drive happens in shaders).
  const vb = uVoiceBright.value;

  // subtle camera parallax from the breathing — barely perceptible
  const breath = Math.sin(uTime.value * (Math.PI * 2 / 4.0)) * 0.5 + 0.5;
  camera.position.x = Math.sin(uTime.value * 0.05) * 0.08;
  camera.position.y = Math.cos(uTime.value * 0.04) * 0.06 + vb * 0.02;
  camera.lookAt(0, 0, 0);

  // nebula slow drift is driven in-shader; nothing to do here
  composer.render();
  requestAnimationFrame(tick);
}
tick();

// Orb voice-brightness is now driven externally from the mic bar
// (see the shell script block above). The shader floor + in-shader
// idle breath keep the orb alive when nothing is setting it.
