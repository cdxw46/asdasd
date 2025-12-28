// Chart configurations and initialization
const Charts = {
    traffic: null,
    cpu: null,
    memory: null,
    connections: null,

    // Data storage for time-series
    data: {
        traffic: {
            timestamps: [],
            values: [],
            maxPoints: 60
        },
        cpu: {
            timestamps: [],
            values: [],
            maxPoints: 60
        },
        memory: {
            timestamps: [],
            values: [],
            maxPoints: 60
        },
        connections: {
            timestamps: [],
            values: [],
            maxPoints: 60
        }
    },

    // Initialize all charts
    init() {
        this.traffic = this.createTrafficChart();
        this.cpu = this.createCPUChart();
        this.memory = this.createMemoryChart();
        this.connections = this.createConnectionsChart();
    },

    // Traffic Overview Chart
    createTrafficChart() {
        const chart = echarts.init(document.getElementById('trafficChart'), 'dark');

        const option = {
            backgroundColor: 'transparent',
            grid: {
                left: '3%',
                right: '4%',
                bottom: '3%',
                top: '10%',
                containLabel: true
            },
            tooltip: {
                trigger: 'axis',
                backgroundColor: 'rgba(15, 20, 35, 0.95)',
                borderColor: '#00d9ff',
                borderWidth: 1,
                textStyle: { color: '#e0e6ed' },
                formatter: '{b}<br/>RPS: {c}'
            },
            xAxis: {
                type: 'category',
                boundaryGap: false,
                data: [],
                axisLine: { lineStyle: { color: '#5a6675' } },
                axisLabel: { color: '#8b95a5', fontSize: 10 },
                splitLine: { show: false }
            },
            yAxis: {
                type: 'value',
                axisLine: { lineStyle: { color: '#5a6675' } },
                axisLabel: { color: '#8b95a5', fontSize: 10 },
                splitLine: { lineStyle: { color: 'rgba(255, 255, 255, 0.05)' } }
            },
            series: [{
                name: 'Requests/sec',
                type: 'line',
                smooth: true,
                symbol: 'none',
                lineStyle: {
                    color: '#ff6b35',
                    width: 2
                },
                areaStyle: {
                    color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                        { offset: 0, color: 'rgba(255, 107, 53, 0.5)' },
                        { offset: 1, color: 'rgba(255, 107, 53, 0.05)' }
                    ])
                },
                data: []
            }]
        };

        chart.setOption(option);
        window.addEventListener('resize', () => chart.resize());
        return chart;
    },

    // CPU Usage Chart
    createCPUChart() {
        const chart = echarts.init(document.getElementById('cpuChart'), 'dark');

        const option = {
            backgroundColor: 'transparent',
            grid: {
                left: '3%',
                right: '4%',
                bottom: '3%',
                top: '10%',
                containLabel: true
            },
            tooltip: {
                trigger: 'axis',
                backgroundColor: 'rgba(15, 20, 35, 0.95)',
                borderColor: '#00d9ff',
                borderWidth: 1,
                textStyle: { color: '#e0e6ed' }
            },
            xAxis: {
                type: 'category',
                boundaryGap: false,
                data: [],
                axisLine: { show: false },
                axisLabel: { show: false },
                splitLine: { show: false }
            },
            yAxis: {
                type: 'value',
                max: 100,
                axisLine: { lineStyle: { color: '#5a6675' } },
                axisLabel: { color: '#8b95a5', fontSize: 10, formatter: '{value}%' },
                splitLine: { lineStyle: { color: 'rgba(255, 255, 255, 0.05)' } }
            },
            series: [{
                name: 'CPU Usage',
                type: 'line',
                smooth: true,
                symbol: 'circle',
                symbolSize: 4,
                lineStyle: {
                    color: '#00d9ff',
                    width: 2
                },
                itemStyle: {
                    color: '#00d9ff'
                },
                areaStyle: {
                    color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                        { offset: 0, color: 'rgba(0, 217, 255, 0.4)' },
                        { offset: 1, color: 'rgba(0, 217, 255, 0.05)' }
                    ])
                },
                data: []
            }]
        };

        chart.setOption(option);
        window.addEventListener('resize', () => chart.resize());
        return chart;
    },

    // Memory Usage Chart
    createMemoryChart() {
        const chart = echarts.init(document.getElementById('memoryChart'), 'dark');

        const option = {
            backgroundColor: 'transparent',
            grid: {
                left: '3%',
                right: '4%',
                bottom: '3%',
                top: '10%',
                containLabel: true
            },
            tooltip: {
                trigger: 'axis',
                backgroundColor: 'rgba(15, 20, 35, 0.95)',
                borderColor: '#00d9ff',
                borderWidth: 1,
                textStyle: { color: '#e0e6ed' }
            },
            xAxis: {
                type: 'category',
                boundaryGap: false,
                data: [],
                axisLine: { show: false },
                axisLabel: { show: false },
                splitLine: { show: false }
            },
            yAxis: {
                type: 'value',
                max: 100,
                axisLine: { lineStyle: { color: '#5a6675' } },
                axisLabel: { color: '#8b95a5', fontSize: 10, formatter: '{value}%' },
                splitLine: { lineStyle: { color: 'rgba(255, 255, 255, 0.05)' } }
            },
            series: [{
                name: 'Memory Usage',
                type: 'line',
                smooth: true,
                symbol: 'circle',
                symbolSize: 4,
                lineStyle: {
                    color: '#00ff88',
                    width: 2
                },
                itemStyle: {
                    color: '#00ff88'
                },
                areaStyle: {
                    color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                        { offset: 0, color: 'rgba(0, 255, 136, 0.4)' },
                        { offset: 1, color: 'rgba(0, 255, 136, 0.05)' }
                    ])
                },
                data: []
            }]
        };

        chart.setOption(option);
        window.addEventListener('resize', () => chart.resize());
        return chart;
    },

    // Connections Chart
    createConnectionsChart() {
        const chart = echarts.init(document.getElementById('connectionsChart'), 'dark');

        const option = {
            backgroundColor: 'transparent',
            grid: {
                left: '3%',
                right: '4%',
                bottom: '3%',
                top: '10%',
                containLabel: true
            },
            tooltip: {
                trigger: 'axis',
                backgroundColor: 'rgba(15, 20, 35, 0.95)',
                borderColor: '#00d9ff',
                borderWidth: 1,
                textStyle: { color: '#e0e6ed' }
            },
            xAxis: {
                type: 'category',
                boundaryGap: false,
                data: [],
                axisLine: { show: false },
                axisLabel: { show: false },
                splitLine: { show: false }
            },
            yAxis: {
                type: 'value',
                axisLine: { lineStyle: { color: '#5a6675' } },
                axisLabel: { color: '#8b95a5', fontSize: 10 },
                splitLine: { lineStyle: { color: 'rgba(255, 255, 255, 0.05)' } }
            },
            series: [{
                name: 'Active Connections',
                type: 'line',
                smooth: true,
                symbol: 'circle',
                symbolSize: 4,
                lineStyle: {
                    color: '#ffaa00',
                    width: 2
                },
                itemStyle: {
                    color: '#ffaa00'
                },
                data: []
            }]
        };

        chart.setOption(option);
        window.addEventListener('resize', () => chart.resize());
        return chart;
    },

    // Update chart with new data
    updateChart(chartName, value) {
        const chart = this[chartName];
        const data = this.data[chartName];

        if (!chart || !data) return;

        // Add new data point
        const now = new Date();
        const timeStr = now.toLocaleTimeString('es-ES', {
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit'
        });

        data.timestamps.push(timeStr);
        data.values.push(value);

        // Keep only maxPoints
        if (data.timestamps.length > data.maxPoints) {
            data.timestamps.shift();
            data.values.shift();
        }

        // Update chart
        chart.setOption({
            xAxis: {
                data: data.timestamps
            },
            series: [{
                data: data.values
            }]
        });
    }
};

// Initialize charts when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => Charts.init());
} else {
    Charts.init();
}
