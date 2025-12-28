// Dashboard WebSocket and UI management
const Dashboard = {
    socket: null,
    peakRPS: 0,
    connectionStatus: null,

    // Initialize dashboard
    init() {
        this.connectionStatus = document.getElementById('connectionStatus');
        this.connectWebSocket();
    },

    // Connect to WebSocket server
    connectWebSocket() {
        this.updateConnectionStatus('connecting');

        this.socket = io({
            reconnection: true,
            reconnectionDelay: 1000,
            reconnectionAttempts: Infinity
        });

        this.socket.on('connect', () => {
            console.log('✅ Connected to server');
            this.updateConnectionStatus('connected');
            // Request immediate metrics update
            this.socket.emit('metrics:request');
        });

        this.socket.on('disconnect', () => {
            console.log('❌ Disconnected from server');
            this.updateConnectionStatus('disconnected');
        });

        this.socket.on('connect_error', (error) => {
            console.error('Connection error:', error);
            this.updateConnectionStatus('disconnected');
        });

        this.socket.on('metrics:update', (metrics) => {
            this.updateDashboard(metrics);
        });
    },

    // Update connection status indicator
    updateConnectionStatus(status) {
        const statusDot = this.connectionStatus.querySelector('.status-dot');
        const statusText = this.connectionStatus.querySelector('.status-text');

        statusDot.className = 'status-dot';

        switch (status) {
            case 'connected':
                statusDot.classList.add('connected');
                statusText.textContent = 'Connected';
                break;
            case 'disconnected':
                statusDot.classList.add('disconnected');
                statusText.textContent = 'Disconnected';
                break;
            case 'connecting':
                statusText.textContent = 'Connecting...';
                break;
        }
    },

    // Update entire dashboard with new metrics
    updateDashboard(metrics) {
        if (!metrics) return;

        // Update header stats
        this.updateElement('totalRequests', this.formatNumber(metrics.requests.total));
        this.updateElement('currentRPS', metrics.requests.rps.current.toFixed(1));

        // Track peak RPS
        if (metrics.requests.rps.current > this.peakRPS) {
            this.peakRPS = metrics.requests.rps.current;
        }
        this.updateElement('peakRPS', `${this.peakRPS.toFixed(1)} RPS`);

        // Update charts
        Charts.updateChart('traffic', metrics.requests.rps.current);
        Charts.updateChart('cpu', metrics.server.cpu.usage);
        Charts.updateChart('memory', metrics.server.memory.percentage);
        Charts.updateChart('connections', metrics.connections.active);

        // Update server stats
        this.updateElement('cpuUsage', `${metrics.server.cpu.usage.toFixed(1)}%`);
        this.updateElement('memoryUsage', `${metrics.server.memory.percentage.toFixed(1)}%`);
        this.updateElement('activeConnections', this.formatNumber(metrics.connections.active));

        // Update request statistics
        this.updateElement('allowedRequests', this.formatNumber(metrics.requests.allowed));
        this.updateElement('blockedRequests', this.formatNumber(metrics.requests.blocked));
        this.updateElement('avgResponse', `${metrics.responseTime.avg}ms`);
        this.updateElement('p95Response', `${metrics.responseTime.p95}ms`);

        // Update tables
        this.updateTopIPsTable(metrics.topIPs);
        this.updateEndpointsTable(metrics.endpoints);
    },

    // Update top IPs table
    updateTopIPsTable(topIPs) {
        const tbody = document.getElementById('topIPsBody');

        if (!topIPs || topIPs.length === 0) {
            tbody.innerHTML = '<tr><td colspan="2" class="no-data">No data yet...</td></tr>';
            return;
        }

        tbody.innerHTML = topIPs.map(item => `
      <tr>
        <td><code>${this.escapeHtml(item.ip)}</code></td>
        <td>${this.formatNumber(item.count)}</td>
      </tr>
    `).join('');
    },

    // Update endpoints table
    updateEndpointsTable(endpoints) {
        const tbody = document.getElementById('endpointsBody');

        if (!endpoints || endpoints.length === 0) {
            tbody.innerHTML = '<tr><td colspan="2" class="no-data">No data yet...</td></tr>';
            return;
        }

        tbody.innerHTML = endpoints.map(item => `
      <tr>
        <td><code>${this.escapeHtml(item.path)}</code></td>
        <td>${this.formatNumber(item.count)}</td>
      </tr>
    `).join('');
    },

    // Update HTML element by ID
    updateElement(id, value) {
        const element = document.getElementById(id);
        if (element) {
            element.textContent = value;
        }
    },

    // Format large numbers
    formatNumber(num) {
        if (num >= 1000000) {
            return (num / 1000000).toFixed(1) + 'M';
        } else if (num >= 1000) {
            return (num / 1000).toFixed(1) + 'K';
        }
        return num.toString();
    },

    // Escape HTML to prevent XSS
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
};

// Initialize dashboard when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => Dashboard.init());
} else {
    Dashboard.init();
}
