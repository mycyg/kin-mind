import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
export function Graph({
  data,
  onSelect,
}: {
  data: any;
  onSelect: (id: string) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [dimension, setDimension] = useState(2);
  const positions = useRef(new Map<string, [number, number, number]>());
  useEffect(() => {
    if (!ref.current || !data?.nodes.length) return;
    const root = ref.current,
      scene = new THREE.Scene();
    scene.background = new THREE.Color("#f4f2ed");
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    root.appendChild(renderer.domElement);
    const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 1000);
    camera.position.set(0, 0, 16);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableRotate = dimension === 3;
    controls.enableDamping = false;
    const geometries: THREE.BufferGeometry[] = [],
      materials: THREE.Material[] = [];
    const nodes: any[] = [];
    const map = new Map<string, THREE.Vector3>();
    const labelElements: {element:HTMLButtonElement;position:THREE.Vector3}[] = [];
    for (const node of data.nodes.filter((n:any)=>data.nodes.length<=24||["finding","work","entity"].includes(n.kind)).slice(0,24)) {
      const element=document.createElement("button");element.className="graph-node-label";
      element.textContent=node.title;element.title=node.title;element.onclick=()=>onSelect(node.id);
      root.appendChild(element);labelElements.push({element,position:new THREE.Vector3()});
      element.dataset.nodeId=node.id;
    }
    for (const node of data.nodes) {
      if (!positions.current.has(node.id)) {
        let hash = 0;
        for (const letter of node.id) hash = (Math.imul(hash, 31) + letter.charCodeAt(0)) >>> 0;
        const angle = (hash % 6283) / 1000, radius = 2 + ((hash >>> 9) % 120) / 10;
        positions.current.set(node.id, node.position ?? [Math.cos(angle) * radius, Math.sin(angle) * radius, (hash % 9) - 4]);
      }
      const p = new THREE.Vector3(
        ...positions.current.get(node.id)!,
      );
      if (dimension === 2) p.z = 0;
      map.set(node.id, p);
      const g = node.kind === "entity" ? new THREE.BoxGeometry(0.34, 0.34, 0.34) : node.kind === "finding" ? new THREE.OctahedronGeometry(0.25) : new THREE.SphereGeometry(0.18, 12, 8),
        m = new THREE.MeshBasicMaterial({
          color:
            node.kind === "entity" ? "#718dbe" : node.kind === "finding" ? "#789a90" : node.kind === "share" ? "#c894a7" : node.kind === "association" ? "#aa83ba" : node.kind === "knowledge"
              ? "#789a90"
              : node.kind === "relationship"
                ? "#c894a7"
                : "#bc765a",
        });
      geometries.push(g);
      materials.push(m);
      const mesh = new THREE.Mesh(g, m);
      mesh.position.copy(p);
      mesh.userData = node;
      scene.add(mesh);
      nodes.push(mesh);
    }
    const bounds = new THREE.Box3().setFromPoints([...map.values()]);
    const center = bounds.getCenter(new THREE.Vector3());
    const radius = Math.max(
      2,
      bounds.getSize(new THREE.Vector3()).length() / 2,
    );
    controls.target.copy(center);
    camera.position
      .copy(center)
      .add(
        new THREE.Vector3(
          0,
          0,
          (radius / Math.tan(THREE.MathUtils.degToRad(25))) * 1.35,
        ),
      );
    controls.update();
    for (const edge of data.edges) {
      const a = map.get(edge.subject),
        b = map.get(edge.object);
      if (a && b) {
        const g = new THREE.BufferGeometry().setFromPoints([a, b]),
          m = edge.layer === "association" ? new THREE.LineDashedMaterial({color: "#aa83ba", dashSize: 0.15, gapSize: 0.12}) : new THREE.LineBasicMaterial({
            color: edge.needs_review ? "#cda576" : "#a2b3aa",
            transparent: true,
            opacity: 0.6,
          });
        geometries.push(g);
        materials.push(m);
        const line = new THREE.Line(g, m);
        line.computeLineDistances();
        line.userData = edge;
        scene.add(line);
        nodes.push(line);
      }
    }
    const draw = () => {
      renderer.render(scene, camera);
      for(const label of labelElements){
        const point=map.get(label.element.dataset.nodeId!);if(!point)continue;
        label.position.copy(point).project(camera);
        label.element.style.left=((label.position.x+1)*root.clientWidth/2)+"px";
        label.element.style.top=((-label.position.y+1)*root.clientHeight/2+8)+"px";
        label.element.hidden=label.position.z>1||Math.abs(label.position.x)>1||Math.abs(label.position.y)>1;
      }
    };
    controls.addEventListener("change", draw);
    const resize = new ResizeObserver(() => {
      const w = root.clientWidth,
        h = root.clientHeight;
      renderer.setSize(w, h);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      draw();
    });
    resize.observe(root);
    let down = { x: 0, y: 0 };
    const pointerdown = (e: PointerEvent) => {
      down = { x: e.clientX, y: e.clientY };
    };
    const select = (e: PointerEvent) => {
      if (Math.hypot(e.clientX - down.x, e.clientY - down.y) > 5) return;
      const r = root.getBoundingClientRect(),
        ray = new THREE.Raycaster();
      ray.params.Line.threshold = 0.12;
      ray.setFromCamera(
        new THREE.Vector2(
          ((e.clientX - r.left) / r.width) * 2 - 1,
          (-(e.clientY - r.top) / r.height) * 2 + 1,
        ),
        camera,
      );
      const hit = ray.intersectObjects(nodes)[0];
      if (hit) onSelect(hit.object.userData.id);
    };
    renderer.domElement.addEventListener("pointerdown", pointerdown);
    renderer.domElement.addEventListener("pointerup", select);
    draw();
    return () => {
      labelElements.forEach(label=>label.element.remove());
      resize.disconnect();
      controls.dispose();
      geometries.forEach((x) => x.dispose());
      materials.forEach((x) => x.dispose());
      renderer.dispose();
      renderer.domElement.remove();
    };
  }, [data, dimension, onSelect]);
  return (
    <div className="graph-wrap">
      <div className="graph-tools">
        <div className="segmented">
          {[2, 3].map((d) => (
            <button
              key={d}
              className={dimension === d ? "selected" : ""}
              onClick={() => setDimension(d)}
            >
              {d}D
            </button>
          ))}
        </div>
        <span>{data?.nodes.length ?? 0} 个节点 · {dimension === 3 ? "拖动旋转" : "右键拖动平移"}，滚动缩放</span>
        <select
          aria-label="按标题读取节点"
          value=""
          onChange={(e) => onSelect(e.target.value)}
        >
          <option value="">读取节点…</option>
          {data?.nodes.map((n: any) => (
            <option key={n.id} value={n.id}>
              {n.title}
            </option>
          ))}
        </select>
      </div>
      <div className="graph" ref={ref} aria-label="事件关系图" />
      {!data?.nodes.length && (
        <div className="graph-empty">
          添加关系或整理主题后，连接会出现在这里。
        </div>
      )}
    </div>
  );
}
