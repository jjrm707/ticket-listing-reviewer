(() => {
  const payload = document.getElementById("chart-data");
  const chart = document.getElementById("price-chart");
  if (!payload || !chart) return;
  const ns = "http://www.w3.org/2000/svg";
  const add = (name, attributes) => {
    const node = document.createElementNS(ns, name);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
    chart.appendChild(node);
    return node;
  };
  try {
    const source = JSON.parse(payload.textContent);
    const datasets = Array.isArray(source.datasets) ? source.datasets.slice(0, 24) : [];
    const points = datasets.flatMap((dataset) =>
      Array.isArray(dataset.data) ? dataset.data.map((point) => ({
        x: Date.parse(point.x), y: Number(point.y)
      })).filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y)) : []
    );
    if (!points.length) return;
    const xs = points.map((point) => point.x);
    const ys = points.map((point) => point.y);
    const minX = Math.min(...xs), maxX = Math.max(...xs);
    const minY = Math.min(...ys), maxY = Math.max(...ys);
    const scaleX = (value) => 55 + ((value - minX) / Math.max(1, maxX - minX)) * 810;
    const scaleY = (value) => 300 - ((value - minY) / Math.max(1, maxY - minY)) * 260;
    add("line", { x1: 55, y1: 20, x2: 55, y2: 300, stroke: "#8ea0b5" });
    add("line", { x1: 55, y1: 300, x2: 865, y2: 300, stroke: "#8ea0b5" });
    const colors = ["#7ce3c5", "#ffca80", "#9cbcff", "#ef93d1", "#b8e986", "#ff8f85"];
    datasets.forEach((dataset, index) => {
      const valid = (Array.isArray(dataset.data) ? dataset.data : []).map((point) => ({
        x: Date.parse(point.x), y: Number(point.y)
      })).filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y));
      if (!valid.length) return;
      add("polyline", {
        points: valid.map((point) => `${scaleX(point.x)},${scaleY(point.y)}`).join(" "),
        fill: "none", stroke: colors[index % colors.length], "stroke-width": 3,
        "stroke-dasharray": Array.isArray(dataset.borderDash) && dataset.borderDash.length ? "7 5" : "none"
      });
      valid.forEach((point) => add("circle", {
        cx: scaleX(point.x), cy: scaleY(point.y), r: 4,
        fill: colors[index % colors.length]
      }));
    });
  } catch (_error) {
    chart.hidden = true;
  }
})();
