(() => {
  const payload = document.getElementById("chart-data");
  const canvas = document.getElementById("price-chart");
  if (!payload || !canvas || typeof Chart === "undefined") return;
  try {
    const source = JSON.parse(payload.textContent);
    const labels = [
      ...new Set(source.datasets.flatMap((dataset) => dataset.data.map((point) => point.x)))
    ].sort();
    const datasets = source.datasets.map((dataset) => {
      const values = new Map(dataset.data.map((point) => [point.x, point.y]));
      return {
        ...dataset,
        data: labels.map((label) => values.get(label) ?? null),
        spanGaps: false,
        tension: 0.15
      };
    });
    new Chart(canvas, {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            display: window.matchMedia("(min-width: 48rem)").matches,
            labels: { boxWidth: 14, boxHeight: 8 }
          }
        },
        scales: {
          x: {
            title: { display: true, text: "Observed at (UTC)" },
            ticks: { maxTicksLimit: 3, maxRotation: 35 }
          },
          y: { title: { display: true, text: "Pair-basis price (USD)" } }
        }
      }
    });
  } catch (_error) {
    canvas.hidden = true;
  }
})();
