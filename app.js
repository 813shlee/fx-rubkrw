let chartInstance = null;

function calcRubKrw(bokUsdKrw, cbrUsdRub) {
    return bokUsdKrw / cbrUsdRub;
}

function scoreSystem(current, avg) {
    const diff = ((current - avg) / avg) * 100;

    if (diff <= -2) return 90;   // 매우 유리
    if (diff <= -1) return 75;
    if (diff <= 1) return 60;
    if (diff <= 3) return 40;
    return 20;                   // 불리
}

function decision(score) {
    if (score >= 80) return "🟢 강력 BUY (환전 유리)";
    if (score >= 60) return "🟡 HOLD";
    return "🔴 WAIT";
}

function format(n) {
    return Math.round(n).toLocaleString("ko-KR");
}

fetch("rates.json?v=" + new Date().getTime())
.then(res => res.json())
.then(data => {

    // -----------------------------
    // 1. 최신값
    // -----------------------------
    const latest = data[data.length - 1];

    const bok = latest.bok_usd_krw;
    const cbr = latest.cbr_usd_rub;

    const calc = calcRubKrw(bok, cbr);

    // -----------------------------
    // 2. UI 값
    // -----------------------------
    document.getElementById("usdRub").innerText = cbr.toFixed(2);
    document.getElementById("krwRub").innerText = calc.toFixed(2);

    document.getElementById("krwToRub").innerText =
        format(1500000 / calc) + " RUB";

    document.getElementById("usdToRub").innerText =
        format(1000 * cbr) + " RUB";

    document.getElementById("today").innerText =
        "오늘 날짜: " + latest.date;

    // -----------------------------
    // 3. 10일 평균
    // -----------------------------
    const calcSeries = data.map(d =>
        calcRubKrw(d.bok_usd_krw, d.cbr_usd_rub)
    );

    const avg10 = calcSeries.slice(-10).reduce((a,b)=>a+b,0) / Math.min(10, calcSeries.length);

    // -----------------------------
    // 4. 점수 시스템 (일자별)
    // -----------------------------
    const scoredData = data.map(d => {
        const c = calcRubKrw(d.bok_usd_krw, d.cbr_usd_rub);
        const s = scoreSystem(c, avg10);
        return {
            ...d,
            calc,
            score: s,
            signal: decision(s)
        };
    });

    const latestScore = scoredData[scoredData.length - 1];

    document.getElementById("status").innerText =
        latestScore.signal + " | 점수: " + latestScore.score;

    // -----------------------------
    // 5. 그래프 (확대 + 여백)
    // -----------------------------
    const ctx = document.getElementById("mainChart");

    if (chartInstance) chartInstance.destroy();

    chartInstance = new Chart(ctx, {
        type: "line",
        data: {
            labels: data.map(d => d.date),
            datasets: [
                {
                    label: "USD/RUB (CBR)",
                    data: data.map(d => d.cbr_usd_rub),
                    borderColor: "#f59e0b",
                    tension: 0.3,
                    pointRadius: 4
                },
                {
                    label: "KRW/RUB (CALC)",
                    data: data.map(d => calcRubKrw(d.bok_usd_krw, d.cbr_usd_rub)),
                    borderColor: "#06b6d4",
                    tension: 0.3,
                    pointRadius: 4,
                    yAxisID: "y1"
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,

            layout: {
                padding: {
                    left: 40,
                    right: 60,
                    top: 20,
                    bottom: 20
                }
            },

            scales: {
                y: {
                    position: "left"
                },
                y1: {
                    position: "right",
                    grid: { drawOnChartArea: false }
                }
            }
        }
    });

    // -----------------------------
    // 6. 테이블 (일자별 점수 포함)
    // -----------------------------
    const table = document.getElementById("dataTable");
    table.innerHTML = "";

    scoredData.slice().reverse().forEach(row => {

        const tr = document.createElement("tr");

        tr.innerHTML = `
            <td>${row.date}</td>
            <td>${row.cbr_usd_rub.toFixed(2)}</td>
            <td>${calcRubKrw(row.bok_usd_krw, row.cbr_usd_rub).toFixed(2)}</td>
            <td>${format(1500000 / calcRubKrw(row.bok_usd_krw, row.cbr_usd_rub))}</td>
            <td>${format(1000 * row.cbr_usd_rub)}</td>
            <td>${row.score}</td>
            <td>${row.signal}</td>
        `;

        table.appendChild(tr);
    });

});

// -----------------------------
// 수동 새로고침
// -----------------------------
function manualRefresh() {
    location.reload();
}