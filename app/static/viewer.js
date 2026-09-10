// Minimal GLB viewer: orbit / rotate / zoom, auto-framing, neutral IBL for PBR.
// Deliberately not an editor - view and inspect only.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

export class Viewer {
  constructor(canvas) {
    this.canvas = canvas;
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.0;

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(45, 1, 0.01, 1000);

    // Neutral studio IBL. Without an environment map, metallic PBR surfaces
    // (which TRELLIS produces plenty of) render essentially black.
    const pmrem = new THREE.PMREMGenerator(this.renderer);
    this.envMap = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    this.scene.environment = this.envMap;
    pmrem.dispose();

    // A little directional light on top of the IBL so form reads clearly.
    const key = new THREE.DirectionalLight(0xffffff, 1.6);
    key.position.set(2.5, 3.5, 2.0);
    this.scene.add(key);
    const fill = new THREE.DirectionalLight(0xffffff, 0.5);
    fill.position.set(-2.5, 0.8, -1.8);
    this.scene.add(fill);
    this.scene.add(new THREE.AmbientLight(0xffffff, 0.18));

    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.07;
    this.controls.enablePan = true;
    this.controls.minDistance = 0.05;
    this.controls.maxDistance = 100;

    this.root = null;
    this._home = null;
    this._loader = new GLTFLoader();

    this._onResize = () => this.resize();
    addEventListener('resize', this._onResize);
    this.resize();
    this._tick = this._tick.bind(this);
    this.renderer.setAnimationLoop(this._tick);
  }

  _tick() {
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }

  resize() {
    const p = this.canvas.parentElement;
    const w = Math.max(1, p.clientWidth), h = Math.max(1, p.clientHeight);
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  clear() {
    if (!this.root) return;
    this.scene.remove(this.root);
    this.root.traverse((o) => {
      if (o.isMesh) {
        o.geometry?.dispose();
        const mats = Array.isArray(o.material) ? o.material : [o.material];
        for (const m of mats) {
          if (!m) continue;
          for (const k of ['map', 'normalMap', 'roughnessMap', 'metalnessMap',
                           'aoMap', 'emissiveMap', 'alphaMap']) m[k]?.dispose?.();
          m.dispose();
        }
      }
    });
    this.root = null;
  }

  async load(url) {
    const gltf = await this._loader.loadAsync(url);
    this.clear();
    this.root = gltf.scene;
    this.scene.add(this.root);
    this.frame();
    return this._describe();
  }

  _describe() {
    let verts = 0, tris = 0, materials = new Set();
    this.root.traverse((o) => {
      if (!o.isMesh) return;
      const g = o.geometry;
      verts += g.attributes.position ? g.attributes.position.count : 0;
      tris += g.index ? g.index.count / 3 : (g.attributes.position?.count ?? 0) / 3;
      (Array.isArray(o.material) ? o.material : [o.material]).forEach((m) => m && materials.add(m.uuid));
    });
    return { vertices: verts, triangles: Math.round(tris), materials: materials.size };
  }

  // Auto-frame: centre the model at the origin and pull the camera back far
  // enough that the bounding sphere fits the vertical FOV with a little margin.
  frame() {
    if (!this.root) return;
    const box = new THREE.Box3().setFromObject(this.root);
    if (box.isEmpty()) return;
    const size = box.getSize(new THREE.Vector3());
    const centre = box.getCenter(new THREE.Vector3());
    this.root.position.sub(centre);

    const radius = Math.max(size.x, size.y, size.z) * 0.5 || 1;
    const fov = THREE.MathUtils.degToRad(this.camera.fov);
    const dist = (radius / Math.sin(fov / 2)) * 1.35;

    this.camera.near = Math.max(dist / 1000, 0.001);
    this.camera.far = dist * 100;
    this.camera.updateProjectionMatrix();

    const dir = new THREE.Vector3(0.8, 0.45, 1).normalize();
    this._home = { pos: dir.multiplyScalar(dist), target: new THREE.Vector3(0, 0, 0) };
    this.reset();
  }

  reset() {
    if (!this._home) return;
    this.camera.position.copy(this._home.pos);
    this.controls.target.copy(this._home.target);
    this.controls.update();
  }

  dispose() {
    removeEventListener('resize', this._onResize);
    this.renderer.setAnimationLoop(null);
    this.clear();
    this.envMap?.dispose();
    this.controls.dispose();
    this.renderer.dispose();
  }
}
