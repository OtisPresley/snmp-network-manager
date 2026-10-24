class SnmpNetworkManagerCard extends HTMLElement {
  constructor() {
    super();
    console.log("%c[SNMP Network Map] %cInitializing card bundle - Version 0.1.2", "color: #ff9800; font-weight: bold;", "color: default;");
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._config = null;
    this._nodes = null;
    this._edges = [];
    this._width = 1000;
    this._height = 600;
    
    // Zoom and Pan state
    this._zoom = 0.95;
    this._panX = 0;
    this._panY = 0;
    this._isDragging = false;
    this._startX = 0;
    this._startY = 0;
    
    // Drag node state
    this._isDraggingNode = false;
    this._draggedNodeId = null;
    this._dragOffsetX = 0;
    this._dragOffsetY = 0;
    
    // Resize Observer for dynamic aspect-ratio sizing
    this._isObservingSvg = false;
    this._resizeObserver = new ResizeObserver(entries => {
      for (let entry of entries) {
        const { width, height } = entry.contentRect;
        if (width > 0 && height > 0) {
          if (Math.abs(this._width - width) > 15 || Math.abs(this._height - height) > 15) {
            console.log("[SNMP Network Map] Resize observed:", width, "x", height);
            const oldWidth = this._width;
            const oldHeight = this._height;
            this._width = width;
            this._height = height;
            if (this._rawNodes && this._rawEdges) {
              // Scale current node positions
              if (this._nodes && Object.keys(this._nodes).length > 0) {
                Object.values(this._nodes).forEach(node => {
                  if (node.x !== undefined && node.y !== undefined) {
                    node.x = node.x * (width / (oldWidth || 1000));
                    node.y = node.y * (height / (oldHeight || 600));
                  }
                });
              }
              this._calculateLayout(this._rawNodes, this._rawEdges, true);
              
              // Dynamically sync viewBox to measured element bounds
              const svgEl = this.shadowRoot.querySelector('svg');
              if (svgEl) {
                svgEl.setAttribute('viewBox', `0 0 ${this._width} ${this._height}`);
              }
              
              // Recalculate default pan centered positions if not overridden by user drag
              if (localStorage.getItem('snmp_map_panX') === null) {
                this._panX = (this._width - this._width * this._zoom) / 2;
              }
              if (localStorage.getItem('snmp_map_panY') === null) {
                this._panY = (this._height - this._height * this._zoom) / 2;
              }
              this._updateTransform();
              
              this._renderGraph();
            }
          }
        }
      }
    });
    
    // Layout mode
    this._layoutMode = 'clustered';
    this._simulationRunning = false;
    this._lastSnapshotId = null;
    this._hasMovedNodes = false;
    this._customLayouts = {};
    
    // List search & sorting state
    this._searchQuery = '';
    this._sortKey = 'name';
    this._sortOrder = 'asc';
    
    this._handleMouseMove = this._handleMouseMove.bind(this);
    this._handleMouseUp = this._handleMouseUp.bind(this);
    this._fetching = false;
  }

  static getConfigElement() {
    return document.createElement("snmp-network-manager-card-editor");
  }

  static getStubConfig() {
    return {
      layout_mode: "clustered",
      fill: true
    };
  }

  setConfig(config) {
    this._config = config;
    this._layoutMode = config.layout_mode || localStorage.getItem('snmp_map_active_layout') || 'clustered';
    this._cardView = config.view || 'map';
    this._updateColors();
    this._renderGraph();
  }

  _updateColors() {
    if (!this.shadowRoot || !this._config) return;
    const cardEl = this.shadowRoot.querySelector('ha-card');
    if (cardEl) {
      const gatewayColor = this._config.gateway_color || '#d500f9';
      const switchColor = this._config.switch_color || '#00e5ff';
      const apColor = this._config.ap_color || '#00e5ff';
      const endpointColor = this._config.endpoint_color || '#00e676';
      const downColor = this._config.down_color || '#ff0000';
      const asleepColor = this._config.asleep_color || '#ffb300';
      const awayColor = this._config.away_color || '#b388ff';
      
      cardEl.style.setProperty('--gateway-color', gatewayColor);
      cardEl.style.setProperty('--switch-color', switchColor);
      cardEl.style.setProperty('--ap-color', apColor);
      cardEl.style.setProperty('--endpoint-color', endpointColor);
      cardEl.style.setProperty('--down-color', downColor);
      cardEl.style.setProperty('--asleep-color', asleepColor);
      cardEl.style.setProperty('--away-color', awayColor);
    }
  }

  connectedCallback() {
    console.log("[SNMP Network Map] connectedCallback() called. hasHass:", !!this._hass);
    if (this._config) {
      this._cardView = this._config.view || 'map';
    }
    if (this._hass && !this._nodes) {
      console.log("[SNMP Network Map] Triggering _fetchGraphData() from connectedCallback().");
      this._fetchGraphData();
    } else {
      if (this._layoutMode === 'force' && this._nodes && Object.keys(this._nodes).length > 0) {
        this._startForceSimulation(this._width, this._height);
      }
      this._renderGraph();
    }
  }

  disconnectedCallback() {
    this._simulationRunning = false;
    window.removeEventListener('mousemove', this._handleMouseMove);
    window.removeEventListener('mouseup', this._handleMouseUp);
    if (this._retryTimeout) clearTimeout(this._retryTimeout);
    if (this._resizeObserver) {
      this._resizeObserver.disconnect();
      this._isObservingSvg = false;
    }
  }

  set hass(hass) {
    const oldHass = this._hass;
    this._hass = hass;
    
    // Check if user navigated back to this view/tab
    const currentPath = window.location.pathname + window.location.search;
    if (this._lastPath && this._lastPath !== currentPath) {
      if (this._config) {
        this._cardView = this._config.view || 'map';
        this._renderGraph();
      }
    }
    this._lastPath = currentPath;
    
    const entityId = 'sensor.snmp_network_topology';
    const entity = this._hass ? this._hass.states[entityId] : null;
    
    let shouldFetch = false;
    if (entity && entity.attributes && entity.attributes.snapshot_id) {
      const snapshotId = entity.attributes.snapshot_id;
      if (snapshotId !== this._lastSnapshotId) {
        this._lastSnapshotId = snapshotId;
        shouldFetch = true;
      }
    }
    
    // Only fetch if connected to the DOM!
    if (this.isConnected) {
      if (!this._nodes || shouldFetch) {
        if (!this._hasMovedNodes) {
          this._fetchGraphData();
        }
      }
    }
    
    // Live update dynamic states, friendly names, and roaming movements without full re-render
    if (this.isConnected && this._nodes && this._domCache && this._domCache.nodes && oldHass) {
      let topologyNeedsRefetch = false;
      Object.entries(this._nodes).forEach(([id, node]) => {
        const entId = node.attrs?.entity_id;
        if (!entId) return;
        const newState = this._hass.states[entId];
        const oldState = oldHass.states[entId];
        if (!newState) return;
        
        // Detect if parent AP / switch connection changed on the entity (client roamed)
        const newAp = newState.attributes?.ap_mac || newState.attributes?.access_point || newState.attributes?.bssid;
        const oldAp = oldState?.attributes?.ap_mac || oldState?.attributes?.access_point || oldState?.attributes?.bssid;
        if (newAp && oldAp && newAp !== oldAp) {
          topologyNeedsRefetch = true;
        }

        // Detect if state changed on the linked entity
        if (oldState && newState.state !== oldState.state) {
          topologyNeedsRefetch = true;
        }

        const nodeEl = this._domCache.nodes[id];
        if (nodeEl) {
          // Update display name if friendly_name changed and node is not an infrastructure device and current name is MAC/IP
          if (node.kind !== 'device') {
            const currentName = node.attrs?.name || '';
            const isMacOrIp = !currentName || /^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$/.test(currentName) || /^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(currentName);
            if (isMacOrIp) {
              const newFn = newState.attributes?.friendly_name;
              const oldFn = oldState?.attributes?.friendly_name;
              if (newFn && newFn !== oldFn) {
                let label = newFn;
                if (label.endsWith(' Restart')) label = label.substring(0, label.length - 8);
                const textEl = nodeEl.querySelector('text');
                if (textEl) {
                  textEl.textContent = label;
                }
              }
            }
          }
        }
      });

      if (topologyNeedsRefetch && !this._hasMovedNodes) {
        if (this._refetchDebounceTimer) {
          clearTimeout(this._refetchDebounceTimer);
        }
        this._refetchDebounceTimer = setTimeout(() => {
          if (!this._fetching) {
            this._fetchGraphData();
          }
        }, 300);
      }
    }

    const card = this.shadowRoot.querySelector('snmp-switch-manager-card');
    if (card) {
      card.hass = this._hass;
    }
    
    // Only call _render() if the SVG is not yet in the DOM.
    // This prevents destroying and rebuilding the DOM on every state update.
    if (!this.shadowRoot.querySelector('svg')) {
      this._render();
    }
  }

  async _fetchGraphData() {
    if (this._fetching) {
      console.log("[SNMP Network Map] _fetchGraphData: Already fetching graph data, skipping duplicate call.");
      return;
    }
    this._fetching = true;
    console.log("[SNMP Network Map] _fetchGraphData() started. isConnected:", this.isConnected);
    try {
      const topologyData = await this._hass.callWS({ type: "snmp_network_manager/get_topology" });
      console.log("[SNMP Network Map] WebSocket response received:", topologyData);
      
      if (!this.isConnected) {
        console.warn("[SNMP Network Map] Element not connected to DOM. Aborting graph data processing.");
        return;
      }
      if (!topologyData || !topologyData.graph) {
        console.warn("[SNMP Network Map] Topology data or graph is missing!");
        return;
      }

      const newNodes = {};
      const newEdges = [];

      // 1. Populate nodes from topology data (already enriched by the server!)
      if (topologyData.graph.nodes) {
        console.log("[SNMP Network Map] Server nodes count:", Object.keys(topologyData.graph.nodes).length);
        Object.entries(topologyData.graph.nodes).forEach(([nodeId, node]) => {
          if (!node) return;
          const attrs = node.attrs || {};
          
          newNodes[nodeId] = {
            kind: node.kind,
            attrs: {
              mac: attrs.mac || '',
              ip: attrs.ip || 'Unknown',
              name: attrs.name || nodeId.split(':').pop(),
              device_id: attrs.device_id || null,
              entity_id: attrs.entity_id || null,
              state: attrs.state || 'up',
              is_ap: attrs.is_ap || false,
              is_gateway: attrs.is_gateway || false,
              integration: attrs.integration || null,
              device_slug: attrs.device_slug || null,
              state_history: attrs.state_history || [],
              last_seen: attrs.last_seen || null
            }
          };
        });

        // Prune any endpoint nodes that duplicate known infrastructure devices
        const infraMacs = new Set();
        Object.entries(newNodes).forEach(([id, n]) => {
          const isAP = n.attrs?.is_ap || (n.attrs?.name || '').startsWith('AP') || id.includes(':ap:');
          const isDevice = n.kind === 'device' || isAP || n.attrs?.is_gateway;
          if (isDevice && n.attrs?.mac) {
            infraMacs.add(n.attrs.mac.replace(/[:-]/g, '').toUpperCase());
          }
        });
        Object.entries(newNodes).forEach(([id, n]) => {
          if (n.kind === 'endpoint') {
            const cleanMac = (n.attrs?.mac || '').replace(/[:-]/g, '').toUpperCase();
            const name = (n.attrs?.name || '').toLowerCase().replace(/[^a-z0-9]/g, '');
            if (cleanMac && infraMacs.has(cleanMac)) {
              console.log("[SNMP Network Map] Pruning duplicate endpoint matching infrastructure MAC:", id);
              delete newNodes[id];
            } else if (name.startsWith('ap') && !id.includes(':ap:')) {
              const isExactApName = Object.values(newNodes).some(other => (other.kind === 'device' || other.attrs?.is_ap) && other.attrs?.name && other.attrs.name.toLowerCase().replace(/[^a-z0-9]/g, '') === name);
              if (isExactApName) {
                console.log("[SNMP Network Map] Pruning duplicate endpoint matching AP name:", id);
                delete newNodes[id];
              }
            }
          }
        });
      }

      // 2. Populate edges from topology data and deduplicate by signature (src, dst, kind)
      if (Array.isArray(topologyData.graph.edges)) {
        console.log("[SNMP Network Map] Server edges count:", topologyData.graph.edges.length);
        const seenEdges = new Set();
        topologyData.graph.edges.forEach(edge => {
          if (edge && edge.src && edge.dst) {
            const sig = `${edge.src}-${edge.dst}-${edge.kind || ''}`;
            if (!seenEdges.has(sig)) {
              seenEdges.add(sig);
              newEdges.push({
                kind: edge.kind,
                src: edge.src,
                dst: edge.dst,
                attrs: {
                  bridge_port: edge.attrs?.bridge_port || 'Link',
                  type: edge.attrs?.type || 'ethernet',
                  ...edge.attrs
                }
              });
            }
          }
        });
      }

      this._customLayouts = this._config.layouts || {};
      
      this._rawNodes = newNodes;
      this._rawEdges = newEdges;
      
      this._edges = newEdges;
      this._nodes = {};
      this._calculateLayout(newNodes, newEdges);
      console.log("[SNMP Network Map] Layout calculated. Final nodes in this._nodes:", Object.keys(this._nodes).length);
      this._renderGraph();
      this._updateToolbar();
    } catch (e) {
      console.error("[SNMP Network Map] Failed to build topology from WebSocket. Error Details:", e, "Code:", e ? e.code : "none", "Message:", e ? e.message : "none", "JSON:", JSON.stringify(e));
      this._lastSnapshotId = null;
      if (this.isConnected) {
        if (this._retryTimeout) clearTimeout(this._retryTimeout);
        this._retryTimeout = setTimeout(() => {
          console.log("[SNMP Network Map] Retrying fetch after WebSocket failure...");
          this._fetchGraphData();
        }, 3000);
      }
    } finally {
      this._fetching = false;
    }
  }

  _calculateLayout(newNodes, newEdges, isResize = false) {
    const allLayouts = { ...this._config?.layouts, ...this._customLayouts };
    const savedLayoutObj = allLayouts[this._layoutMode] || {};
    
    // Support base layout property
    const savedLayout = savedLayoutObj.positions || savedLayoutObj;
    const baseLayout = savedLayoutObj.base || 'clustered';
    
    const fill = this._config.fill !== false;
    const width = fill ? this._width : (this._config.width || 1000);
    const height = fill ? this._height : (this._config.height || 600);
    
    const isCustom = !['clustered', 'concentric', 'force', 'grid'].includes(this._layoutMode);
    const layoutToRun = isCustom ? baseLayout : this._layoutMode;
    
    if (layoutToRun !== 'force' || !isResize) {
      this._nodes = {};
    }
    
    if (layoutToRun === 'concentric') {
      this._calculateConcentricLayout(newNodes, width, height);
    } else if (layoutToRun === 'force') {
      if (!isResize) {
        this._calculateClusteredLayout(newNodes, newEdges, width, height);
      }
      this._startForceSimulation(width, height);
    } else if (layoutToRun === 'grid') {
      this._calculateGridLayout(newNodes, newEdges, width, height);
    } else {
      this._calculateClusteredLayout(newNodes, newEdges, width, height);
    }
    
    // Apply saved positions
    if (isCustom) {
      Object.entries(savedLayout).forEach(([id, pos]) => {
        if (this._nodes[id]) {
          this._nodes[id].x = pos.x;
          this._nodes[id].y = pos.y;
        }
      });
    }
  }

  _calculateGridLayout(newNodes, newEdges, width, height) {
    // 1. BFS to determine parentId of each node based on newEdges
    const visited = new Set();
    const roots = [];
    
    // Identify gateways/routers/firewalls as primary roots
    Object.keys(newNodes).forEach(id => {
      const node = newNodes[id];
      const name = node.attrs?.name || id.split(':').pop();
      if (node.kind === 'device' && (
        node.attrs?.is_gateway ||
        name.toLowerCase().includes('gateway') || 
        name.toLowerCase().includes('router') || 
        name.toLowerCase().includes('firewall')
      )) {
        roots.push(id);
        visited.add(id);
      }
    });
    
    // Build adjacency list for BFS
    const neighbors = {};
    Object.keys(newNodes).forEach(id => neighbors[id] = []);
    
    (newEdges || []).forEach(edge => {
      if (newNodes[edge.src] && newNodes[edge.dst]) {
        neighbors[edge.src].push({ to: edge.dst });
        neighbors[edge.dst].push({ to: edge.src });
      }
    });
    
    const queue = [...roots];
    while (queue.length > 0) {
      const current = queue.shift();
      const nbs = neighbors[current] || [];
      nbs.sort((a, b) => {
        const typeA = newNodes[a.to]?.kind || '';
        const typeB = newNodes[b.to]?.kind || '';
        if (typeA === 'device' && typeB !== 'device') return -1;
        if (typeA !== 'device' && typeB === 'device') return 1;
        return 0;
      });
      
      nbs.forEach(nb => {
        if (!visited.has(nb.to)) {
          visited.add(nb.to);
          newNodes[nb.to].parentId = current;
          queue.push(nb.to);
        }
      });
    }
    
    // Secondary roots for disconnected devices
    Object.keys(newNodes).forEach(id => {
      const node = newNodes[id];
      if (!visited.has(id) && node.kind === 'device') {
        roots.push(id);
        visited.add(id);
        
        const subQueue = [id];
        while (subQueue.length > 0) {
          const current = subQueue.shift();
          const nbs = neighbors[current] || [];
          nbs.sort((a, b) => {
            const typeA = newNodes[a.to]?.kind || '';
            const typeB = newNodes[b.to]?.kind || '';
            if (typeA === 'device' && typeB !== 'device') return -1;
            if (typeA !== 'device' && typeB === 'device') return 1;
            return 0;
          });
          
          nbs.forEach(nb => {
            if (!visited.has(nb.to)) {
              visited.add(nb.to);
              newNodes[nb.to].parentId = current;
              subQueue.push(nb.to);
            }
          });
        }
      }
    });

    // 2. Categorize devices into gateways and parents
    const gateways = [];
    const parents = [];
    const endpoints = [];
    
    Object.entries(newNodes).forEach(([id, node]) => {
      const name = node.attrs?.name || id.split(':').pop();
      const n = { ...node, id };
      if (node.kind === 'device') {
        const isGateway = node.attrs?.is_gateway || name.toLowerCase().includes('gateway') || name.toLowerCase().includes('router') || name.toLowerCase().includes('firewall');
        if (isGateway) {
          gateways.push(n);
        } else {
          parents.push(n);
        }
      } else {
        endpoints.push(n);
      }
    });

    parents.sort((a, b) => (a.attrs?.name || a.id).localeCompare(b.attrs?.name || b.id));
    gateways.sort((a, b) => (a.attrs?.name || a.id).localeCompare(b.attrs?.name || b.id));

    // Group endpoints by parentId
    const endpointsByParent = {};
    const orphanEndpoints = [];
    
    endpoints.forEach(ep => {
      const pId = ep.parentId;
      if (pId) {
        if (!endpointsByParent[pId]) {
          endpointsByParent[pId] = [];
        }
        endpointsByParent[pId].push(ep);
      } else {
        orphanEndpoints.push(ep);
      }
    });

    // Sort endpoints alphabetically within their parent's list
    Object.keys(endpointsByParent).forEach(pId => {
      endpointsByParent[pId].sort((a, b) => (a.attrs?.name || a.id).localeCompare(b.attrs?.name || b.id));
    });
    orphanEndpoints.sort((a, b) => (a.attrs?.name || a.id).localeCompare(b.attrs?.name || b.id));

    // Calculate dynamic rows target
    const targetRows = Math.max(1, Math.min(5, Math.ceil(Math.sqrt(endpoints.length / 2))));

    // Allocate columns horizontally
    const columnAllocations = [];
    let currentColumnStart = 1;
    
    const allocate = (device, isOrphans = false) => {
      const eps = isOrphans ? orphanEndpoints : (endpointsByParent[device.id] || []);
      let colsForDevice = 0;
      if (eps.length > 0) {
        colsForDevice = Math.ceil(eps.length / targetRows);
      } else if (!isOrphans) {
        colsForDevice = 1;
      }
      
      if (colsForDevice > 0) {
        columnAllocations.push({
          id: isOrphans ? 'orphans' : device.id,
          device: isOrphans ? null : device,
          isOrphans,
          startCol: currentColumnStart,
          endCol: currentColumnStart + colsForDevice - 1,
          eps: eps
        });
        currentColumnStart += colsForDevice;
      }
    };
    
    gateways.forEach(g => allocate(g));
    parents.forEach(p => allocate(p));
    if (orphanEndpoints.length > 0) {
      allocate(null, true);
    }
    
    const totalCols = currentColumnStart - 1;
    const colSpacing = totalCols > 0 ? (width / (totalCols + 1)) : width / 2;

    // Position each device and its endpoints
    columnAllocations.forEach((alloc, idx) => {
      const centerCol = (alloc.startCol + alloc.endCol) / 2;
      const xPos = colSpacing * centerCol;
      
      const yFactor = height / 1000;
      if (alloc.device) {
        newNodes[alloc.device.id].x = xPos;
        const isGateway = gateways.some(g => g.id === alloc.device.id);
        if (isGateway) {
          newNodes[alloc.device.id].y = 150 * yFactor;
        } else {
          // Stagger parents
          newNodes[alloc.device.id].y = ((idx % 2 === 0) ? 310 : 370) * yFactor;
        }
      }
      
      const eps = alloc.eps;
      if (eps.length > 0) {
        const localCols = alloc.endCol - alloc.startCol + 1;
        eps.forEach((ep, epIdx) => {
          const colOffset = epIdx % localCols;
          const rowIdx = Math.floor(epIdx / localCols);
          const epCol = alloc.startCol + colOffset;
          
          newNodes[ep.id].x = colSpacing * epCol;
          newNodes[ep.id].y = (550 + rowIdx * 100) * yFactor;
        });
      }
    });

    // Make sure all nodes are mapped back to this._nodes
    this._nodes = {};
    Object.entries(newNodes).forEach(([id, node]) => {
      this._nodes[id] = {
        ...node,
        id,
        x: node.x || 500,
        y: node.y || 500
      };
    });
  }

  _calculateConcentricLayout(newNodes, width, height) {
    const center_x = width / 2;
    const center_y = height / 2;
    const gateways = []; const switches = []; const aps = []; const endpoints = [];
    
    Object.entries(newNodes).forEach(([id, node]) => {
      const name = node.attrs?.name || id.split(':').pop();
      const n = { ...node, id };
      if (node.kind === 'device') {
        const isGateway = n.attrs?.is_gateway || name.toLowerCase().includes('gateway') || name.toLowerCase().includes('router') || name.toLowerCase().includes('firewall');
        const isAP = (n.attrs && n.attrs.is_ap) || name.startsWith('AP') || name.toLowerCase().startsWith('ap ') || name.toLowerCase().startsWith('ap-') || name.toLowerCase() === 'ap';
        if (isGateway) gateways.push(n);
        else if (isAP) aps.push(n);
        else switches.push(n);
      } else if (node.kind === 'endpoint') endpoints.push(n);
    });
    
    aps.sort((a, b) => a.attrs.name.localeCompare(b.attrs.name));
    endpoints.sort((a, b) => (a.attrs.name || '').localeCompare(b.attrs.name || ''));
    
    gateways.forEach((node, i) => { node.x = center_x; node.y = center_y - 50 + (i * 50); this._nodes[node.id] = node; });
    
    const switchRadius = Math.min(width, height) * 0.18;
    switches.forEach((node, i) => {
      const angle = (2 * Math.PI * i) / (switches.length || 1);
      node.x = center_x + switchRadius * Math.cos(angle);
      node.y = center_y + switchRadius * Math.sin(angle);
      this._nodes[node.id] = node;
    });
    
    const apRadius = Math.min(width, height) * 0.33;
    aps.forEach((node, i) => {
      const angle = (2 * Math.PI * i) / (aps.length || 1);
      node.x = center_x + apRadius * Math.cos(angle);
      node.y = center_y + apRadius * Math.sin(angle);
      this._nodes[node.id] = node;
    });
    
    const endpointRadius = Math.min(width, height) * 0.47;
    endpoints.forEach((node, i) => {
      const angle = (2 * Math.PI * i) / (endpoints.length || 1);
      node.x = center_x + endpointRadius * Math.cos(angle);
      node.y = center_y + endpointRadius * Math.sin(angle);
      this._nodes[node.id] = node;
    });
  }

  _calculateClusteredLayout(newNodes, newEdges, width, height) {
    const center_x = width / 2;
    const center_y = height / 2;
    const aspectRatio = width / height;
    
    this._nodes = {};
    
    const adj = {}; // parent -> list of children
    const visited = new Set();
    const roots = [];
    
    // Step 1: Identify gateways/routers/firewalls as primary roots
    Object.keys(newNodes).forEach(id => {
      const node = newNodes[id];
      const name = node.attrs?.name || id.split(':').pop();
      if (node.kind === 'device' && (
        node.attrs?.is_gateway ||
        name.toLowerCase().includes('gateway') || 
        name.toLowerCase().includes('router') || 
        name.toLowerCase().includes('firewall')
      )) {
        roots.push(id);
        visited.add(id);
      }
    });
    
    // Step 2: Build an undirected adjacency list of connections for BFS traversal.
    // This allows us to find parent-child directions dynamically based on path distance from root.
    const neighbors = {};
    Object.keys(newNodes).forEach(id => neighbors[id] = []);
    
    newEdges.forEach(edge => {
      if (newNodes[edge.src] && newNodes[edge.dst]) {
        neighbors[edge.src].push({ to: edge.dst, kind: edge.kind });
        neighbors[edge.dst].push({ to: edge.src, kind: edge.kind });
      }
    });
    
    // BFS queue starting with primary roots
    const queue = [...roots];
    
    while (queue.length > 0) {
      const current = queue.shift();
      const nbs = neighbors[current] || [];
      
      // Sort neighbors: traverse device nodes (switches/APs) before endpoints.
      // This helps group devices together in the tree hierarchy.
      nbs.sort((a, b) => {
        const typeA = newNodes[a.to]?.kind || '';
        const typeB = newNodes[b.to]?.kind || '';
        if (typeA === 'device' && typeB !== 'device') return -1;
        if (typeA !== 'device' && typeB === 'device') return 1;
        return 0;
      });
      
      nbs.forEach(nb => {
        if (!visited.has(nb.to)) {
          visited.add(nb.to);
          if (!adj[current]) adj[current] = [];
          adj[current].push(nb.to);
          newNodes[nb.to].parentId = current;
          queue.push(nb.to);
        }
      });
    }
    
    // Step 3: Find secondary roots (e.g. disconnected switches or APs)
    Object.keys(newNodes).forEach(id => {
      const node = newNodes[id];
      if (!visited.has(id) && node.kind === 'device') {
        roots.push(id);
        visited.add(id);
        
        const subQueue = [id];
        while (subQueue.length > 0) {
          const current = subQueue.shift();
          const nbs = neighbors[current] || [];
          nbs.sort((a, b) => {
            const typeA = newNodes[a.to]?.kind || '';
            const typeB = newNodes[b.to]?.kind || '';
            if (typeA === 'device' && typeB !== 'device') return -1;
            if (typeA !== 'device' && typeB === 'device') return 1;
            return 0;
          });
          
          nbs.forEach(nb => {
            if (!visited.has(nb.to)) {
              visited.add(nb.to);
              if (!adj[current]) adj[current] = [];
              adj[current].push(nb.to);
              newNodes[nb.to].parentId = current;
              subQueue.push(nb.to);
            }
          });
        }
      }
    });
    
    // Step 4: Track orphan/unconnected nodes
    const orphans = [];
    Object.keys(newNodes).forEach(id => {
      if (!visited.has(id)) {
        orphans.push(id);
      }
    });
    
    // Step 5: Helper function to recursively count descendants for sector weighting
    const descendantCounts = {};
    const getDescendantCount = (nodeId) => {
      if (descendantCounts[nodeId] !== undefined) {
        return descendantCounts[nodeId];
      }
      const children = adj[nodeId] || [];
      if (children.length === 0) {
        descendantCounts[nodeId] = 1;
        return 1;
      }
      let count = 0;
      children.forEach(childId => {
        count += getDescendantCount(childId);
      });
      descendantCounts[nodeId] = count;
      return count;
    };
    
    // Step 6: Recursive positioning function
    const positionSubtree = (nodeId, depth, parentX, parentY, startAngle, endAngle) => {
      const node = { ...newNodes[nodeId], id: nodeId };
      const midAngle = (startAngle + endAngle) / 2;
      
      let r = 0;
      if (depth > 0) {
        const isAP = (node.attrs && node.attrs.is_ap) || nodeId.includes(':ap:') || (node.attrs?.name && (node.attrs.name.startsWith('AP') || node.attrs.name.toLowerCase().startsWith('ap ') || node.attrs.name.toLowerCase().startsWith('ap-') || node.attrs.name.toLowerCase() === 'ap'));
        const isSwitch = node.kind === 'device' && !isAP;
        
        // Dynamic radius steps scaled based on height
        const radiusStep = Math.min(width, height) * 0.16;
        const minEndpointRadius = Math.min(width, height) * 0.42;
        
        if (isSwitch) {
          r = depth * radiusStep;
        } else if (isAP) {
          r = depth * radiusStep;
        } else {
          // Push endpoints to a larger radius to avoid overlapping switch levels
          r = Math.max(depth * radiusStep, minEndpointRadius);
        }
      }
      
      if (depth === 0) {
        node.x = parentX;
        node.y = parentY;
      } else {
        node.x = center_x + r * Math.cos(midAngle);
        node.y = center_y + r * Math.sin(midAngle);
      }
      
      // Jitter slightly to handle exact overlaps
      node.x += (Math.random() - 0.5) * 2;
      node.y += (Math.random() - 0.5) * 2;
      
      // Keep within bounds
      node.x = Math.max(50, Math.min(width - 50, node.x));
      node.y = Math.max(50, Math.min(height - 50, node.y));
      
      this._nodes[nodeId] = node;
      
      const children = adj[nodeId] || [];
      if (children.length === 0) return;
      
      // Sort children alphabetically by name/friendly name to keep layout deterministic
      children.sort((a, b) => {
        const nameA = newNodes[a]?.attrs?.name || a.split(':').pop();
        const nameB = newNodes[b]?.attrs?.name || b.split(':').pop();
        return nameA.localeCompare(nameB);
      });
      
      // If all children are endpoints (e.g. AP clients or switch access port clients),
      // arrange them in a clean circular cluster directly around this parent device node!
      const allChildrenEndpoints = children.every(cId => newNodes[cId]?.kind === 'endpoint');
      if (allChildrenEndpoints && children.length > 0) {
        const numChildren = children.length;
        const clusterRadius = Math.min(65 + numChildren * 2.5, 120);
        children.forEach((childId, idx) => {
          const angle = (2 * Math.PI * idx) / numChildren;
          const childNode = { ...newNodes[childId], id: childId };
          childNode.x = node.x + clusterRadius * Math.cos(angle);
          childNode.y = node.y + clusterRadius * Math.sin(angle);
          childNode.x = Math.max(30, Math.min(width - 30, childNode.x));
          childNode.y = Math.max(30, Math.min(height - 30, childNode.y));
          this._nodes[childId] = childNode;
        });
        return;
      }

      const weights = children.map(childId => getDescendantCount(childId));
      const totalWeight = weights.reduce((sum, w) => sum + w, 0);
      
      let currentAngle = startAngle;
      const sectorWidth = endAngle - startAngle;
      
      children.forEach((childId, idx) => {
        const weight = weights[idx];
        const childSectorWidth = sectorWidth * (weight / totalWeight);
        positionSubtree(childId, depth + 1, node.x, node.y, currentAngle, currentAngle + childSectorWidth);
        currentAngle += childSectorWidth;
      });
    };
    
    // Step 7: Lay out root nodes
    if (roots.length === 1) {
      positionSubtree(roots[0], 0, center_x, center_y, 0, 2 * Math.PI);
    } else if (roots.length > 1) {
      const rootRadius = 100;
      // Calculate total weights of all roots to distribute sectors proportionally
      const rootWeights = roots.map(rId => getDescendantCount(rId));
      const totalRootWeight = rootWeights.reduce((sum, w) => sum + w, 0);
      
      let currentAngle = 0;
      roots.forEach((rootId, idx) => {
        const weight = rootWeights[idx];
        const sectorWidth = 2 * Math.PI * (weight / totalRootWeight);
        const midAngle = currentAngle + sectorWidth / 2;
        
        const rx = center_x + rootRadius * Math.cos(midAngle);
        const ry = center_y + rootRadius * Math.sin(midAngle);
        
        const node = { ...newNodes[rootId], id: rootId, x: rx, y: ry };
        this._nodes[rootId] = node;
        
        const children = adj[rootId] || [];
        if (children.length > 0) {
          const childWeights = children.map(cId => getDescendantCount(cId));
          const totalChildWeight = childWeights.reduce((sum, w) => sum + w, 0);
          
          let childAngle = currentAngle;
          children.forEach((childId, cIdx) => {
            const cWeight = childWeights[cIdx];
            const childSectorWidth = sectorWidth * (cWeight / totalChildWeight);
            positionSubtree(childId, 1, rx, ry, childAngle, childAngle + childSectorWidth);
            childAngle += childSectorWidth;
          });
        }
        
        currentAngle += sectorWidth;
      });
    }
    
    // Step 8: Lay out orphan/unconnected nodes in a neat grid at the bottom
    if (orphans.length > 0) {
      orphans.sort((a, b) => {
        const nameA = newNodes[a]?.attrs?.name || a.split(':').pop();
        const nameB = newNodes[b]?.attrs?.name || b.split(':').pop();
        return nameA.localeCompare(nameB);
      });
      
      const colWidth = 70;
      const rowHeight = 40;
      const margin = 50;
      const usableWidth = width - 2 * margin;
      const cols = Math.max(1, Math.floor(usableWidth / colWidth));
      
      orphans.forEach((id, idx) => {
        const col = idx % cols;
        const row = Math.floor(idx / cols);
        const node = { ...newNodes[id], id };
        
        node.x = margin + col * colWidth + (colWidth / 2);
        node.y = height - margin - row * rowHeight;
        node.isOrphan = true;
        node.fixed = true;
        
        this._nodes[id] = node;
      });
    }
  }

  _calculateRandomLayout(newNodes, width, height) {
    Object.entries(newNodes).forEach(([id, node]) => {
      this._nodes[id] = { ...node, id, x: Math.random() * (width - 200) + 100, y: Math.random() * (height - 200) + 100, vx: 0, vy: 0 };
    });
  }

  _startForceSimulation(width, height) {
    if (!this.isConnected) return;
    this._simulationId = (this._simulationId || 0) + 1;
    const currentSimId = this._simulationId;
    this._simulationRunning = true;
    const center_x = width / 2;
    const center_y = height / 2;
    const k = Math.sqrt((width * height) / (Object.keys(this._nodes).length || 1)) * 1.5;
    
    this._alpha = 1.0;
    
    const step = () => {
      if (!this.isConnected || this._simulationId !== currentSimId) {
        return;
      }
      if (!this._simulationRunning) {
        return;
      }
      
      const gravity = 0.015;
      
      // 1. Repulsion force and gravity
      Object.values(this._nodes).forEach(v => {
        if (v.fixed || v.id === this._draggedNodeId) return;
        
        if (v.vx === undefined) v.vx = 0;
        if (v.vy === undefined) v.vy = 0;
        
        // Apply gravity towards center
        v.vx += (center_x - v.x) * gravity * this._alpha;
        v.vy += (center_y - v.y) * gravity * this._alpha;
        
        v.vx *= 0.8; v.vy *= 0.8;
        
        Object.values(this._nodes).forEach(u => {
          if (v !== u) {
            // Let all nodes repel each other to avoid overlapping and improve force layout organization
            let shouldRepel = true;
            if (!shouldRepel) return;
            
            const dx = v.x - u.x; const dy = v.y - u.y;
            const distSq = dx * dx + dy * dy;
            if (distSq < 0.01) return;
            
            const k2 = k * k;
            const maxDistSq = k2 * 6.25; // (2.5 * k)^2
            const minSilence = (v.kind === 'device' ? 24 : 12) + (u.kind === 'device' ? 24 : 12) + 15;
            const minSilenceSq = minSilence * minSilence;
            
            // Only calculate Math.sqrt if we actually need it for force or collision
            if (distSq < maxDistSq || distSq < minSilenceSq) {
              const dist = Math.sqrt(distSq);
              
              if (distSq < maxDistSq) {
                const force = k2 / distSq;
                v.vx += (dx / dist) * force * 0.5 * this._alpha;
                v.vy += (dy / dist) * force * 0.5 * this._alpha;
              }
              
              if (distSq < minSilenceSq) {
                const overlap = minSilence - dist;
                v.vx += (dx / dist) * overlap * 0.15 * this._alpha;
                v.vy += (dy / dist) * overlap * 0.15 * this._alpha;
              }
            }
          }
        });
      });
      
      // 2. Attraction force (spring force along edges)
      this._edges.forEach(edge => {
        const v = this._nodes[edge.src]; const u = this._nodes[edge.dst];
        if (v && u) {
          if (v.fixed || u.fixed) return;
          
          const dx = v.x - u.x; const dy = v.y - u.y;
          const dist = Math.sqrt(dx * dx + dy * dy) || 1;
          
          const targetDist = (v.kind === 'device' && u.kind === 'device') ? 100 : 70;
          const strength = 0.04;
          const force = (dist - targetDist) * strength;
          
          v.vx -= (dx / dist) * force * this._alpha;
          v.vy -= (dy / dist) * force * this._alpha;
          u.vx += (dx / dist) * force * this._alpha;
          u.vy += (dy / dist) * force * this._alpha;
        }
      });
      
      // 3. Update coordinates and clamp within bounds
      Object.values(this._nodes).forEach(node => {
        if (node.fixed || node.id === this._draggedNodeId) return;
        
        const maxV = 20;
        const vSpeed = Math.sqrt(node.vx * node.vx + node.vy * node.vy);
        if (vSpeed > maxV) {
          node.vx = (node.vx / vSpeed) * maxV;
          node.vy = (node.vy / vSpeed) * maxV;
        }
        
        node.x += node.vx; node.y += node.vy;
        node.x = Math.max(40, Math.min(width - 40, node.x));
        node.y = Math.max(40, Math.min(height - 40, node.y));
      });
      
      // 4. Update the DOM elements in-place!
      this._updateDOMPositions();
      
      // Decay alpha
      this._alpha *= 0.97;
      
      if (this._alpha < 0.05) {
        this._simulationRunning = false;
      } else {
        requestAnimationFrame(step);
      }
    };
    requestAnimationFrame(step);
  }

  _updateDOMPositions() {
    if (!this._domCache) return;
    
    this._edges.forEach(edge => {
      const src = this._nodes[edge.src]; const dst = this._nodes[edge.dst];
      if (!src || !dst) return;
      
      const edgeKey = `edge-${edge.src.replace(/:/g, '_')}-${edge.dst.replace(/:/g, '_')}`;
      const hoverKey = `edge-hover-${edge.src.replace(/:/g, '_')}-${edge.dst.replace(/:/g, '_')}`;
      
      const line = this._domCache.edges[edgeKey];
      const hoverLine = this._domCache.edges[hoverKey];
      
      if (line) {
        line.setAttribute('x1', src.x);
        line.setAttribute('y1', src.y);
        line.setAttribute('x2', dst.x);
        line.setAttribute('y2', dst.y);
      }
      if (hoverLine) {
        hoverLine.setAttribute('x1', src.x);
        hoverLine.setAttribute('y1', src.y);
        hoverLine.setAttribute('x2', dst.x);
        hoverLine.setAttribute('y2', dst.y);
      }
    });

    Object.values(this._nodes).forEach(node => {
      const nodeEl = this._domCache.nodes[node.id];
      if (nodeEl) {
        nodeEl.setAttribute('transform', `translate(${node.x},${node.y})`);
      }
    });
  }

  _setView(view) {
    this._cardView = view;
    this._config = { ...this._config, view: view };
    
    // Fire config-changed event to save the active view mode to dashboard card configuration
    const event = new CustomEvent("config-changed", {
      detail: { config: this._config },
      bubbles: true,
      composed: true
    });
    this.dispatchEvent(event);
    
    // Update local displays instantly in the DOM
    const wrapper = this.shadowRoot.getElementById('map-wrapper');
    const list = this.shadowRoot.getElementById('list-container');
    const grouped = this.shadowRoot.getElementById('grouped-container');
    
    if (wrapper) wrapper.style.display = view === 'map' ? 'block' : 'none';
    if (list) list.style.display = view === 'list' ? 'block' : 'none';
    if (grouped) grouped.style.display = view === 'grouped' ? 'block' : 'none';
    
    // Toggle active status for view buttons
    ['map', 'list', 'grouped'].forEach(v => {
      const btn = this.shadowRoot.getElementById(`btn-view-${v}`);
      if (btn) {
        if (v === view) btn.classList.add('active');
        else btn.classList.remove('active');
      }
    });

    this._renderGraph();
  }

  _setLayout(mode) {
    this._layoutMode = mode;
    localStorage.setItem('snmp_map_active_layout', mode);
    this._simulationRunning = false;
    this._hasMovedNodes = false;
    
    if (this._rawNodes && this._rawEdges) {
      this._nodes = {};
      this._calculateLayout(this._rawNodes, this._rawEdges);
      this._renderGraph();
      this._updateToolbar();
    } else {
      const entityId = 'sensor.snmp_network_topology';
      const entity = this._hass.states[entityId];
      if (entity) {
        this._fetchGraphData();
      }
    }
    
    // Explicitly call _render to update layout mode buttons active states
    this._render();
  }

  _render() {
    if (!this._hass || !this._config) return;

    const allLayouts = { ...this._config.layouts, ...this._customLayouts };
    const customLayoutNames = Object.keys(allLayouts).filter(m => !['clustered', 'concentric', 'force', 'grid'].includes(m));
    
    const fill = this._config.fill !== false;
    const width = fill ? this._width : (this._config.width || 1000);
    const height = fill ? this._height : (this._config.height || 600);
    
    let svgStyle = '';
    if (fill) {
      svgStyle = 'width: 100%; height: 100%; min-height: 500px;';
    } else {
      svgStyle = `width: ${width}px; height: ${height}px;`;
    }

    const gatewayColor = this._config.gateway_color || '#d500f9';
    const switchColor = this._config.switch_color || '#00e5ff';
    const apColor = this._config.ap_color || '#00e5ff';
    const endpointColor = this._config.endpoint_color || '#00e676';
    const downColor = this._config.down_color || '#ff0000';
    const asleepColor = this._config.asleep_color || '#ffb300';
    const awayColor = this._config.away_color || '#b388ff';

    if (!this.shadowRoot.querySelector('svg')) {
      this.shadowRoot.innerHTML = `
        <style>
          ha-card {
            padding: 16px;
            color: var(--primary-text-color);
            background: var(--ha-card-background, var(--card-background-color, white));
            border-radius: var(--ha-card-border-radius, 12px);
            box-shadow: var(--ha-card-box-shadow, none);
            font-family: var(--paper-font-common-base_-_font-family, sans-serif);
            overflow: hidden;
            position: relative;
            display: flex;
            flex-direction: column;
            height: 100%;
            box-sizing: border-box;
          }
          .title { font-size: 1.2rem; font-weight: bold; margin-bottom: 8px; }
          .summary-bar { display: flex; gap: 24px; padding: 12px 16px; background: rgba(255,255,255,0.02); border: 1px solid var(--divider-color); border-radius: 6px; margin-bottom: 16px; font-size: 0.9rem; flex-wrap: wrap; align-items: center; }
          .summary-item { display: flex; align-items: center; gap: 6px; }
          .summary-icon { color: var(--primary-color); --mdc-icon-size: 18px; }
          .toolbar { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; align-items: center; }
          .toolbar button {
            background: var(--primary-color); color: var(--text-primary-color);
            border: none; padding: 6px 12px; border-radius: 4px;
            cursor: pointer; font-weight: 500; transition: opacity 0.2s;
          }
          .toolbar button:hover { opacity: 0.9; }
          .toolbar button.active { background: #ff9800 !important; color: white !important; }
          .toolbar button.btn-danger { background: var(--error-color) !important; color: white !important; }
          
          #map-wrapper {
            flex-grow: 1;
            width: 100%;
            height: 100%;
            min-height: 500px;
            position: relative;
          }
          svg {
            position: absolute;
            top: 0; left: 0;
            width: 100%; height: 100%;
            border-radius: 8px;
            background: rgba(0,0,0,0.2); box-shadow: inset 0 0 20px rgba(0,0,0,0.5);
            cursor: grab;
          }
          svg:active { cursor: grabbing; }
          .node { cursor: pointer; }
          .node circle { fill: #1a1a1a; stroke-width: 2px; transition: stroke 0.3s; }
          .node.unlinked circle {
            stroke: var(--error-color, #ff5252) !important;
            stroke-dasharray: 3,2;
            stroke-width: 2px !important;
            filter: drop-shadow(0 0 4px var(--error-color, #ff5252)) !important;
          }
          
          .node.gateway circle { stroke: var(--gateway-color); filter: drop-shadow(0 0 5px var(--gateway-color)); }
          .node.switch circle { stroke: var(--switch-color); filter: drop-shadow(0 0 5px var(--switch-color)); }
          .node.ap circle { stroke: var(--ap-color); filter: drop-shadow(0 0 5px var(--ap-color)); }
          .node.endpoint circle {
            fill: var(--endpoint-color);
            stroke: var(--ha-card-background, var(--card-background-color, #1a1a1a));
            stroke-width: 1.5px;
            filter: drop-shadow(0 0 3px var(--endpoint-color));
          }
          .node.endpoint.down circle {
            fill: var(--down-color) !important;
            filter: drop-shadow(0 0 3px var(--down-color)) !important;
            opacity: 0.6;
          }
          .node.endpoint.asleep circle {
            fill: var(--asleep-color) !important;
            filter: drop-shadow(0 0 3px var(--asleep-color)) !important;
            opacity: 0.8;
          }
          .node.endpoint.away circle {
            fill: var(--away-color) !important;
            filter: drop-shadow(0 0 3px var(--away-color)) !important;
            opacity: 0.8;
          }
          
          .node.down circle { stroke: var(--down-color) !important; opacity: 0.6; }
          .node.down text { fill: var(--down-color); }
          
          .node.asleep circle { stroke: var(--asleep-color) !important; opacity: 0.8; }
          .node.asleep text { fill: var(--asleep-color); }
          
          .node.away circle { stroke: var(--away-color) !important; opacity: 0.8; }
          .node.away text { fill: var(--away-color); }
          
          .edge { stroke: #fff; stroke-opacity: 0.15; stroke-width: 1.5px; }
          .edge.lldp { stroke: #ff9800; stroke-dasharray: 8,4; animation: flow 1s linear infinite; stroke-opacity: 0.5; }
          .edge.fdb { stroke: #00e5ff; stroke-opacity: 0.6; stroke-width: 2px; }
          
          .edge.speed-10g { stroke: #d500f9 !important; stroke-width: 3.5px !important; stroke-opacity: 0.8 !important; }
          .edge.speed-1g { stroke: var(--primary-color, #00e5ff) !important; stroke-width: 2px !important; stroke-opacity: 0.6 !important; }
          .edge.speed-100m { stroke: #ffab00 !important; stroke-width: 1.2px !important; stroke-opacity: 0.8 !important; stroke-dasharray: 4,3 !important; }
          
          @keyframes flow { to { stroke-dashoffset: -12; } }
          
          text {
            font-size: 11px; pointer-events: none;
            fill: var(--primary-text-color); text-shadow: 0 1px 3px rgba(0,0,0,0.8);
            font-weight: 500;
          }
          
          .modal {
            display: none;
            position: fixed;
            top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0,0,0,0.7);
            z-index: 1000;
            justify-content: center;
            align-items: center;
          }
          .modal-content {
            background: var(--ha-card-background, var(--card-background-color, white));
            padding: 20px;
            border-radius: 8px;
            width: 90%;
            max-width: 800px;
            max-height: 90%;
            overflow-y: auto;
            position: relative;
          }
          .close {
            position: absolute;
            top: 10px; right: 15px;
            font-size: 24px;
            cursor: pointer;
            color: var(--primary-text-color);
          }
        </style>
        <ha-card>
          <div class="title">SNMP Network Topology Map</div>
          <div class="toolbar" id="toolbar">
            <button id="btn-view-map" class="${this._cardView === 'map' ? 'active' : ''}">Map</button>
            <button id="btn-view-list" class="${this._cardView === 'list' ? 'active' : ''}">List</button>
            <button id="btn-view-grouped" class="${this._cardView === 'grouped' ? 'active' : ''}">Grouped</button>
            
            <span class="layout-control">|</span>
            <button id="btn-clustered" class="layout-control ${this._layoutMode === 'clustered' ? 'active' : ''}">Clustered</button>
            <button id="btn-concentric" class="layout-control ${this._layoutMode === 'concentric' ? 'active' : ''}">Concentric</button>
            <button id="btn-grid" class="layout-control ${this._layoutMode === 'grid' ? 'active' : ''}">Grid</button>
            <button id="btn-force" class="layout-control ${this._layoutMode === 'force' ? 'active' : ''}">Force</button>
            
            <span id="custom-buttons" class="layout-control"></span>
            
            <span class="layout-control">|</span>
            <button id="btn-zoom-in" class="layout-control">Zoom +</button>
            <button id="btn-zoom-out" class="layout-control">Zoom -</button>
            <button id="btn-reset-zoom" class="layout-control">Reset</button>
            <span class="layout-control">|</span>
            <button id="btn-save" class="layout-control" disabled>Save Layout</button>
            <button id="btn-cancel" class="layout-control" style="display: none; background: var(--secondary-background-color) !important; color: var(--primary-text-color) !important;">Cancel</button>
            <button id="btn-delete" class="layout-control btn-danger" style="display: none;">Delete Layout</button>
          </div>
          <div id="map-summary-bar" class="summary-bar" style="display: none;"></div>
          <div id="map-wrapper" style="${svgStyle} display: ${this._cardView === 'map' ? 'block' : 'none'};">
            <svg viewBox="0 0 ${width} ${height}">
              <defs>
                <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">
                  <path d="M 40 0 L 0 0 0 40" fill="none" stroke="rgba(255,255,255,0.03)" stroke-width="1"/>
                </pattern>
              </defs>
              <rect width="100%" height="100%" fill="url(#grid)" />
              <g id="viewport">
                <g id="edges"></g>
                <g id="nodes"></g>
              </g>
            </svg>
          </div>
          <div id="list-container" style="display: ${this._cardView === 'list' ? 'block' : 'none'}; padding: 15px;"></div>
          <div id="grouped-container" style="display: ${this._cardView === 'grouped' ? 'block' : 'none'}; padding: 15px;"></div>
          
          <div id="modal" class="modal">
            <div class="modal-content" style="padding: 0;">
              <div style="display: flex; justify-content: space-between; align-items: center; padding: 10px; background: rgba(0,0,0,0.2);">
                <div id="modal-title" style="font-weight: bold; padding-left: 10px;">Switch Configuration</div>
                <div style="display: flex; align-items: center; padding-right: 10px;">
                  <ha-icon id="modal-edit" icon="mdi:cog" style="cursor: pointer; margin-right: 30px;"></ha-icon>
                  <span class="close" style="position: static; cursor: pointer; font-size: 28px; line-height: 1;">&times;</span>
                </div>
              </div>
              <div id="modal-body" style="padding: 15px;"></div>
              <div id="modal-edit-body" style="display: none; padding: 15px;">
                <div style="text-align: right; margin-bottom: 10px;">
                  <button id="modal-save" style="background: var(--accent-color); color: white; border: none; padding: 5px 15px; border-radius: 4px; cursor: pointer;">Save Settings</button>
                </div>
                <div id="modal-editor-container"></div>
              </div>
            </div>
          </div>
        </ha-card>
      `;
      
      this._setupZoomPan();
      this._setupEvents();
    }
    
    this._updateColors();
    
    const wrapper = this.shadowRoot.getElementById('map-wrapper');
    if (wrapper) {
      wrapper.setAttribute('style', `${svgStyle} display: ${this._cardView === 'map' ? 'block' : 'none'};`);
    }
    const svg = this.shadowRoot.querySelector('svg');
    if (svg) {
      svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    }
    
    // Start observing wrapper size
    setTimeout(() => {
      const wrapperEl = this.shadowRoot.getElementById('map-wrapper');
      if (wrapperEl && !this._isObservingSvg && this._resizeObserver) {
        this._resizeObserver.observe(wrapperEl);
        this._isObservingSvg = true;
      }
    }, 0);
    
    const customSpan = this.shadowRoot.getElementById('custom-buttons');
    if (customSpan) {
      customSpan.innerHTML = customLayoutNames.map(m => `
        <button class="btn-custom ${this._layoutMode === m ? 'active' : ''}" data-mode="${m}">${m}</button>
      `).join('');
      
      this.shadowRoot.querySelectorAll('.btn-custom').forEach(btn => {
        btn.addEventListener('click', () => this._setLayout(btn.dataset.mode));
      });
    }
    
    this.shadowRoot.querySelectorAll('.toolbar button').forEach(btn => {
      if (btn.id === `btn-${this._layoutMode}`) {
        btn.classList.add('active');
      } else if (!btn.classList.contains('btn-custom')) {
        btn.classList.remove('active');
      }
    });
    
    const deleteBtn = this.shadowRoot.getElementById('btn-delete');
    if (deleteBtn) {
      const isCustom = !['clustered', 'concentric', 'force', 'grid'].includes(this._layoutMode);
      deleteBtn.style.display = isCustom ? 'inline-block' : 'none';
    }
    
    if (!this._simulationRunning) {
      this._renderGraph();
    }
  }

  _updateTransform() {
    const viewport = this.shadowRoot.getElementById('viewport');
    if (viewport) {
      viewport.setAttribute('transform', `translate(${this._panX}, ${this._panY}) scale(${this._zoom})`);
    }
  }

  _handleMouseMove(e) {
    if (this._isDraggingNode) {
      const node = this._nodes[this._draggedNodeId];
      if (node) {
        const svgEl = this.shadowRoot.querySelector('svg');
        if (!svgEl) return;
        const rect = svgEl.getBoundingClientRect();
        const mouseX = (e.clientX - rect.left - this._panX) / this._zoom;
        const mouseY = (e.clientY - rect.top - this._panY) / this._zoom;
        
        node.x = mouseX - this._dragOffsetX;
        node.y = mouseY - this._dragOffsetY;
        
        this._hasMovedNodes = true;
        this._updateToolbar();
        
        // Update dragged node position in-place
        const nodeEl = this._domCache ? this._domCache.nodes[node.id] : null;
        if (nodeEl) {
          nodeEl.setAttribute('transform', `translate(${node.x},${node.y})`);
        }
        
        // Update connected edges in-place
        this._edges.forEach(edge => {
          if (edge.src === node.id || edge.dst === node.id) {
            const src = this._nodes[edge.src];
            const dst = this._nodes[edge.dst];
            if (src && dst) {
              const edgeKey = `edge-${edge.src.replace(/:/g, '_')}-${edge.dst.replace(/:/g, '_')}`;
              const hoverKey = `edge-hover-${edge.src.replace(/:/g, '_')}-${edge.dst.replace(/:/g, '_')}`;
              
              const line = this._domCache ? this._domCache.edges[edgeKey] : null;
              const hoverLine = this._domCache ? this._domCache.edges[hoverKey] : null;
              
              if (line) {
                line.setAttribute('x1', src.x);
                line.setAttribute('y1', src.y);
                line.setAttribute('x2', dst.x);
                line.setAttribute('y2', dst.y);
              }
              if (hoverLine) {
                hoverLine.setAttribute('x1', src.x);
                hoverLine.setAttribute('y1', src.y);
                hoverLine.setAttribute('x2', dst.x);
                hoverLine.setAttribute('y2', dst.y);
              }
            }
          }
        });
      }
    } else if (this._isDragging) {
      this._panX = e.clientX - this._startX;
      this._panY = e.clientY - this._startY;
      this._updateTransform();
      
      localStorage.setItem('snmp_map_panX', this._panX);
      localStorage.setItem('snmp_map_panY', this._panY);
    }
  }

  _handleMouseUp() {
    this._isDraggingNode = false;
    this._isDragging = false;
    window.removeEventListener('mousemove', this._handleMouseMove);
    window.removeEventListener('mouseup', this._handleMouseUp);
  }

  _setupZoomPan() {
    const svg = this.shadowRoot.querySelector('svg');
    
    const fill = this._config?.fill !== false;
    const width = fill ? 1000 : (this._config?.width || 1000);
    const height = fill ? 600 : (this._config?.height || 600);
    
    this._zoom = parseFloat(localStorage.getItem('snmp_map_zoom')) || 0.95;
    
    const defaultPanX = (width - width * this._zoom) / 2;
    const defaultPanY = (height - height * this._zoom) / 2;
    
    this._panX = localStorage.getItem('snmp_map_panX') !== null ? parseFloat(localStorage.getItem('snmp_map_panX')) : defaultPanX;
    this._panY = localStorage.getItem('snmp_map_panY') !== null ? parseFloat(localStorage.getItem('snmp_map_panY')) : defaultPanY;
    
    this._updateTransform();

    svg.addEventListener('mousedown', (e) => {
      if (e.target.closest('.node')) return;
      this._isDragging = true;
      this._startX = e.clientX - this._panX;
      this._startY = e.clientY - this._panY;
      
      window.addEventListener('mousemove', this._handleMouseMove);
      window.addEventListener('mouseup', this._handleMouseUp);
    });

    svg.addEventListener('wheel', (e) => {
      e.preventDefault();
      const zoomFactor = 1.1; const oldZoom = this._zoom;
      if (e.deltaY < 0) this._zoom *= zoomFactor;
      else this._zoom /= zoomFactor;
      this._zoom = Math.max(0.2, Math.min(this._zoom, 5));
      const rect = svg.getBoundingClientRect();
      const mouseX = e.clientX - rect.left; const mouseY = e.clientY - rect.top;
      this._panX = mouseX - (mouseX - this._panX) * (this._zoom / oldZoom);
      this._panY = mouseY - (mouseY - this._panY) * (this._zoom / oldZoom);
      this._updateTransform();
      
      localStorage.setItem('snmp_map_zoom', this._zoom);
      localStorage.setItem('snmp_map_panX', this._panX);
      localStorage.setItem('snmp_map_panY', this._panY);
    }, { passive: false });
  }

  _setupEvents() {
    this.shadowRoot.getElementById('btn-view-map').addEventListener('click', () => this._setView('map'));
    this.shadowRoot.getElementById('btn-view-list').addEventListener('click', () => this._setView('list'));
    this.shadowRoot.getElementById('btn-view-grouped').addEventListener('click', () => this._setView('grouped'));

    this.shadowRoot.getElementById('btn-clustered').addEventListener('click', () => this._setLayout('clustered'));
    this.shadowRoot.getElementById('btn-concentric').addEventListener('click', () => this._setLayout('concentric'));
    this.shadowRoot.getElementById('btn-force').addEventListener('click', () => this._setLayout('force'));
    this.shadowRoot.getElementById('btn-grid').addEventListener('click', () => this._setLayout('grid'));
    
    this.shadowRoot.getElementById('btn-zoom-in').addEventListener('click', () => { this._zoom *= 1.2; this._zoom = Math.min(this._zoom, 5); this._updateTransform(); localStorage.setItem('snmp_map_zoom', this._zoom); });
    this.shadowRoot.getElementById('btn-zoom-out').addEventListener('click', () => { this._zoom /= 1.2; this._zoom = Math.max(0.2, this._zoom); this._updateTransform(); localStorage.setItem('snmp_map_zoom', this._zoom); });
    this.shadowRoot.getElementById('btn-reset-zoom').addEventListener('click', () => { 
      this._zoom = 0.95; 
      const fill = this._config?.fill !== false;
      const width = fill ? this._width : (this._config?.width || 1000);
      const height = fill ? this._height : (this._config?.height || 600);
      this._panX = (width - width * 0.95) / 2; 
      this._panY = (height - height * 0.95) / 2; 
      this._updateTransform(); 
      localStorage.removeItem('snmp_map_zoom');
      localStorage.removeItem('snmp_map_panX');
      localStorage.removeItem('snmp_map_panY');
    });
    
    this.shadowRoot.getElementById('btn-save').addEventListener('click', () => this._saveLayoutToApi());
    
    this.shadowRoot.getElementById('btn-cancel').addEventListener('click', () => {
      this._hasMovedNodes = false;
      this._fetchGraphData();
    });
    
    this.shadowRoot.getElementById('btn-delete').addEventListener('click', () => this._deleteLayoutFromApi());
    
    const nodesG = this.shadowRoot.getElementById('nodes');
    
    nodesG.addEventListener('click', (e) => {
      if (this._hasMovedNodes) return;
      
      try {
        const nodeG = e.target.closest('.node');
        if (nodeG) {
          const id = nodeG.dataset.id;
          const node = this._nodes[id];
          if (node) {
            const name = (node.attrs?.name || '').toLowerCase();
            const isAP = node.attrs?.is_ap || name.startsWith('ap ') || name.startsWith('ap-') || name === 'ap' || id.includes(':ap:');
            if (node.attrs?.integration === 'snmp_switch_manager' && !isAP) {
              this._openSwitchCard(node, id);
            } else {
              this._openDeviceDetailsCard(node, id);
            }
          }
        }
      } catch (err) {
        alert("Map Node Click Error: " + err.message + "\n" + err.stack);
        console.error(err);
      }
    });
    
    nodesG.addEventListener('mousedown', (e) => {
      const nodeG = e.target.closest('.node');
      if (nodeG) {
        this._isDraggingNode = true;
        this._draggedNodeId = nodeG.dataset.id;
        const node = this._nodes[this._draggedNodeId];
        const svgEl = this.shadowRoot.querySelector('svg');
        const rect = svgEl.getBoundingClientRect();
        const mouseX = (e.clientX - rect.left - this._panX) / this._zoom;
        const mouseY = (e.clientY - rect.top - this._panY) / this._zoom;
        this._dragOffsetX = mouseX - node.x;
        this._dragOffsetY = mouseY - node.y;
        
        window.addEventListener('mousemove', this._handleMouseMove);
        window.addEventListener('mouseup', this._handleMouseUp);
        e.stopPropagation();
      }
    });
  }

  _updateToolbar() {
    const saveBtn = this.shadowRoot.getElementById('btn-save');
    const cancelBtn = this.shadowRoot.getElementById('btn-cancel');
    const deleteBtn = this.shadowRoot.getElementById('btn-delete');
    
    if (saveBtn) saveBtn.disabled = !this._hasMovedNodes;
    if (cancelBtn) cancelBtn.style.display = (this._cardView === 'map' && this._hasMovedNodes) ? 'inline-block' : 'none';
    if (deleteBtn) {
      const isCustom = !['clustered', 'concentric', 'force', 'grid'].includes(this._layoutMode);
      deleteBtn.style.display = (this._cardView === 'map' && isCustom) ? 'inline-block' : 'none';
    }
  }

  async _saveLayoutToApi() {
    const name = prompt("Enter a name for this custom layout:", this._layoutMode);
    if (!name) return;
    
    const layoutData = {};
    Object.entries(this._nodes).forEach(([id, node]) => {
      layoutData[id] = { x: node.x, y: node.y };
    });
    
    const baseLayout = ['clustered', 'concentric', 'force', 'grid'].includes(this._layoutMode) ? this._layoutMode : 'clustered';
    
    const layouts = { ...this._config.layouts };
    layouts[name] = { base: baseLayout, positions: layoutData };
    
    this._config = {
      ...this._config,
      layouts: layouts
    };
    
    this._hasMovedNodes = false;
    this._layoutMode = name;
    
    // Dispatch config-changed event to persist custom layout in dashboard config
    const event = new CustomEvent("config-changed", {
      detail: { config: this._config },
      bubbles: true,
      composed: true
    });
    this.dispatchEvent(event);
    this._fetchGraphData();
  }

  async _deleteLayoutFromApi() {
    if (!confirm(`Are you sure you want to delete the layout "${this._layoutMode}"?`)) return;
    
    const layouts = { ...this._config.layouts };
    if (layouts[this._layoutMode]) {
      delete layouts[this._layoutMode];
    }
    
    this._config = {
      ...this._config,
      layouts: layouts
    };
    
    this._layoutMode = 'clustered';
    
    // Dispatch config-changed event to persist deletion in dashboard config
    const event = new CustomEvent("config-changed", {
      detail: { config: this._config },
      bubbles: true,
      composed: true
    });
    this.dispatchEvent(event);
    this._fetchGraphData();
  }

  _openSwitchCard(node) {
    if (!customElements.get('snmp-switch-manager-card')) {
      const entityId = node.attrs.entity_id;
      if (entityId) {
        const event = new CustomEvent('hass-more-info', {
          detail: { entityId: entityId },
          bubbles: true,
          composed: true
        });
        this.dispatchEvent(event);
      } else {
        alert("SNMP Switch Manager Card not found, and no correlated entity found for this device.");
      }
      return;
    }

    const modal = this.shadowRoot.getElementById('modal');
    const body = this.shadowRoot.getElementById('modal-body');
    const editBtn = this.shadowRoot.getElementById('modal-edit');
    const editBody = this.shadowRoot.getElementById('modal-edit-body');
    const editorContainer = this.shadowRoot.getElementById('modal-editor-container');
    const saveBtn = this.shadowRoot.getElementById('modal-save');
    const titleEl = this.shadowRoot.querySelector('#modal-title');
    
    modal.style.display = 'flex';
    body.innerHTML = '';
    if (editorContainer) editorContainer.innerHTML = '';
    editBody.style.display = 'none';
    body.style.display = 'block';
    if (editBtn) {
      editBtn.style.display = 'block';
      editBtn.icon = 'mdi:cog';
    }
    if (titleEl) titleEl.textContent = 'Switch Configuration';
    
    const card = document.createElement('snmp-switch-manager-card');
    const deviceSlug = node.attrs.device_slug || node.attrs.name.toLowerCase().replace(/[-\.]/g, '_');
    
    let baseConfig = {
      type: "custom:snmp-switch-manager-card",
      title: "",
      view: "panel",
      color_mode: "state",
      ports_per_row: 24,
      panel_width: 740,
      port_size: 18,
      show_labels: true,
      label_numbers_only: false,
      label_size: 8,
      info_position: "below",
      hide_diagnostics: false,
      hide_virtual_interfaces: false,
      calibration_mode: false,
      device: deviceSlug,
      horizontal_port_gap: 10,
      vertical_port_gap: 10
    };
    
    // Load from config
    const switchConfigs = this._config?.switch_configs || {};
    const userOverrides = switchConfigs[deviceSlug] || {};
    const config = { ...baseConfig, ...userOverrides, device: deviceSlug };
    
    card.setConfig(config);
    card.hass = this._hass;
    body.appendChild(card);
    
    let pendingConfig = { ...config };
    
    // Setup Edit Click
    editBtn.onclick = () => {
      if (editBody.style.display === 'none') {
        body.style.display = 'none';
        editBody.style.display = 'block';
        editBtn.icon = 'mdi:eye';
        
        // Create editor
        const editor = document.createElement('snmp-switch-manager-card-editor');
        editor.hass = this._hass;
        editor.setConfig(config);
        
        // Listen for config changes
        editor.addEventListener('config-changed', (ev) => {
          pendingConfig = ev.detail.config;
          // Update the preview card!
          card.setConfig(pendingConfig);
        });
        
        editorContainer.innerHTML = '';
        editorContainer.appendChild(editor);
      } else {
        body.style.display = 'block';
        editBody.style.display = 'none';
        editBtn.icon = 'mdi:cog';
      }
    };
    
    // Setup Save Click
    if (saveBtn) {
      saveBtn.onclick = () => {
        const switchConfigs = { ...this._config?.switch_configs };
        switchConfigs[deviceSlug] = pendingConfig;
        
        this._config = {
          ...this._config,
          switch_configs: switchConfigs
        };
        
        // Dispatch config-changed event to persist switch configs in dashboard config
        const event = new CustomEvent("config-changed", {
          detail: { config: this._config },
          bubbles: true,
          composed: true
        });
        this.dispatchEvent(event);
        alert("Settings saved successfully!");
        
        // Toggle back to view mode
        body.style.display = 'block';
        editBody.style.display = 'none';
        editBtn.icon = 'mdi:cog';
      };
    }
    
    const closeBtn = this.shadowRoot.querySelector('.close');
    closeBtn.onclick = () => { modal.style.display = 'none'; body.innerHTML = ''; };
    modal.onclick = (e) => { if (e.target === modal) { modal.style.display = 'none'; body.innerHTML = ''; } };
  }

  _openDeviceDetailsCard(node, id) {
    const modal = this.shadowRoot.getElementById('modal');
    const body = this.shadowRoot.getElementById('modal-body');
    const titleEl = this.shadowRoot.querySelector('#modal-title');
    const editBtn = this.shadowRoot.getElementById('modal-edit');
    const editBody = this.shadowRoot.getElementById('modal-edit-body');
    
    modal.style.display = 'flex';
    body.innerHTML = '';
    if (editBody) editBody.style.display = 'none';
    body.style.display = 'block';
    
    if (editBtn) editBtn.style.display = 'none';
    if (titleEl) titleEl.textContent = 'Device Details';

    const nodeId = id || node.id || '';
    const name = node.attrs?.name || nodeId.split(':').pop() || 'Unknown';
    const mac = node.attrs?.mac || 'Unknown';
    const ip = node.attrs?.ip || 'Unknown';
    const state = node.attrs?.state || 'down';
    const kind = node.kind === 'device' ? (node.attrs?.is_ap ? 'Access Point' : 'Network Infrastructure') : 'Client Endpoint';
    const deviceId = node.attrs?.device_id;
    
    let stateLabel = 'Offline';
    let stateClass = 'offline';
    if (state === 'up') {
      stateLabel = 'Online';
      stateClass = 'online';
    } else if (state === 'asleep') {
      stateLabel = 'Asleep';
      stateClass = 'asleep';
    } else if (state === 'away') {
      stateLabel = 'Away';
      stateClass = 'away';
    }

    let parentName = 'Unknown';
    let portName = 'Unknown';
    this._edges.forEach(edge => {
      if (edge.src === edge.dst) return; // Skip self-loops
      if (edge.dst === nodeId && edge.kind === 'lldp') {
        const parent = this._nodes[edge.src];
        if (parent) parentName = parent.attrs?.name || 'Unknown';
        portName = edge.attrs?.local_port || edge.attrs?.port_name || edge.attrs?.bridge_port || 'Unknown';
      } else if (edge.src === nodeId && edge.kind === 'fdb') {
        const parent = this._nodes[edge.dst];
        if (parent) parentName = parent.attrs?.name || 'Unknown';
        portName = edge.attrs?.bridge_port || 'Unknown';
      }
    });

    let lastSeenStr = 'Never';
    if (node.attrs?.last_seen) {
      const lastSeenDate = new Date(node.attrs.last_seen * 1000);
      const diffSecs = Math.floor((Date.now() - lastSeenDate.getTime()) / 1000);
      let relativeStr = '';
      if (diffSecs < 60) {
        relativeStr = 'just now';
      } else if (diffSecs < 3600) {
        const mins = Math.floor(diffSecs / 60);
        relativeStr = `${mins} minute${mins > 1 ? 's' : ''} ago`;
      } else if (diffSecs < 86400) {
        const hours = Math.floor(diffSecs / 3600);
        relativeStr = `${hours} hour${hours > 1 ? 's' : ''} ago`;
      } else {
        const days = Math.floor(diffSecs / 86400);
        relativeStr = `${days} day${days > 1 ? 's' : ''} ago`;
      }
      lastSeenStr = `${lastSeenDate.toLocaleString()} (${relativeStr})`;
    }

    // State duration calculation
    const history = node.attrs?.state_history || [];
    let durationStr = 'Unknown';
    if (history.length > 0) {
      const lastEvent = history[history.length - 1];
      const elapsedSec = Math.max(0, Math.floor(Date.now() / 1000 - lastEvent.timestamp));
      if (elapsedSec < 60) {
        durationStr = `${elapsedSec}s`;
      } else if (elapsedSec < 3600) {
        durationStr = `${Math.floor(elapsedSec / 60)}m`;
      } else if (elapsedSec < 86400) {
        durationStr = `${Math.floor(elapsedSec / 3600)}h ${Math.floor((elapsedSec % 3600) / 60)}m`;
      } else {
        durationStr = `${Math.floor(elapsedSec / 86400)}d ${Math.floor((elapsedSec % 86400) / 3600)}h`;
      }
    }

    function formatTime(timestamp) {
      const date = new Date(timestamp * 1000);
      return date.toLocaleString();
    }

    const getStateClass = (s) => {
      const val = (s || '').toLowerCase();
      if (val === 'up') return 'online';
      if (val === 'down') return 'offline';
      if (val === 'asleep') return 'asleep';
      if (val === 'away') return 'away';
      return val;
    };
    const formatStateLabel = (s) => {
      const val = (s || '').toLowerCase();
      if (val === 'up') return 'Online';
      if (val === 'down') return 'Offline';
      if (val === 'asleep') return 'Asleep';
      if (val === 'away') return 'Away';
      return s;
    };

    const asleepColor = this._config?.asleep_color || '#ffb300';
    const awayColor = this._config?.away_color || '#b388ff';

    // Construct history sections
    let historyHtml = '';
    if (history.length > 0) {
      const reversedHistory = [...history].reverse();
      const quickHistory = reversedHistory.slice(0, 3);
      const remainingHistory = reversedHistory.slice(3);
      
      historyHtml = `
        <div class="history-section" style="margin-top: 18px;">
          <div style="font-weight: bold; font-size: 0.95rem; color: var(--primary-color); margin-bottom: 8px; border-bottom: 1px solid var(--divider-color); padding-bottom: 4px;">State History</div>
          <div class="history-list" style="display: flex; flex-direction: column; gap: 6px;">
            ${quickHistory.map(h => {
              const badgeClass = `details-badge ${getStateClass(h.state)}`;
              return `
                <div style="display: flex; justify-content: space-between; align-items: center; font-size: 0.85rem; padding: 6px 10px; background: rgba(255,255,255,0.02); border-radius: 4px; border: 1px solid var(--divider-color);">
                  <span class="${badgeClass}" style="padding: 2px 6px; border-radius: 4px; font-size: 0.7rem;">${formatStateLabel(h.state)}</span>
                  <span style="color: var(--secondary-text-color); font-family: monospace;">${formatTime(h.timestamp)}</span>
                </div>
              `;
            }).join('')}
          </div>
          ${remainingHistory.length > 0 ? `
            <details style="margin-top: 10px; font-size: 0.85rem;">
              <summary style="cursor: pointer; color: var(--primary-color); user-select: none; font-weight: bold; padding: 4px 0;">Show Expanded History (${remainingHistory.length} more)</summary>
              <div style="display: flex; flex-direction: column; gap: 6px; margin-top: 6px; padding-left: 8px; border-left: 2px solid var(--divider-color);">
                ${remainingHistory.map(h => {
                  const badgeClass = `details-badge ${getStateClass(h.state)}`;
                  return `
                    <div style="display: flex; justify-content: space-between; align-items: center; font-size: 0.8rem; padding: 2px 0;">
                      <span class="${badgeClass}" style="padding: 1px 4px; border-radius: 4px; font-size: 0.65rem;">${formatStateLabel(h.state)}</span>
                      <span style="color: var(--secondary-text-color); font-family: monospace;">${formatTime(h.timestamp)}</span>
                    </div>
                  `;
                }).join('')}
              </div>
            </details>
          ` : ''}
        </div>
      `;
    } else {
      historyHtml = `
        <div style="font-size: 0.85rem; color: var(--secondary-text-color); font-style: italic; text-align: center; margin-top: 16px;">
          No state history recorded yet.
        </div>
      `;
    }

    const linkedIntegrations = node.attrs?.linked_integrations || [];
    let linkedIntegrationsHtml = '';
    if (linkedIntegrations.length > 0) {
      linkedIntegrationsHtml = `
        <div style="display: flex; gap: 6px; flex-wrap: wrap; align-items: center;">
          ${linkedIntegrations.map(integration => {
            const domain = integration.domain;
            const title = integration.title || domain;
            return `
              <div style="display: inline-flex; align-items: center; gap: 6px; background: rgba(255, 255, 255, 0.05); border: 1px solid rgba(255, 255, 255, 0.1); padding: 4px 8px; border-radius: 12px; font-size: 0.8rem; line-height: 1;">
                <img src="https://brands.home-assistant.io/_/${domain}/icon.png" style="width: 16px; height: 16px; object-fit: contain;" onerror="this.style.display='none';" />
                <span>${title}</span>
              </div>
            `;
          }).join('')}
        </div>
      `;
    } else {
      linkedIntegrationsHtml = `<span style="font-style: italic; color: var(--secondary-text-color); font-size: 0.85rem;">None</span>`;
    }

    body.innerHTML = `
      <style>
        .details-container {
          color: var(--primary-text-color);
          font-family: sans-serif;
        }
        .details-header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          margin-bottom: 20px;
        }
        .details-title {
          font-size: 1.3rem;
          font-weight: bold;
        }
        .details-badge {
          padding: 4px 10px;
          border-radius: 12px;
          font-size: 0.8rem;
          font-weight: bold;
        }
        .details-badge.online { background: rgba(0, 230, 118, 0.15); color: #00e676; }
        .details-badge.offline { background: rgba(255, 0, 0, 0.15); color: #ff0000; }
        .details-badge.asleep { background: rgba(255, 179, 0, 0.15); color: ${asleepColor}; }
        .details-badge.away { background: rgba(179, 136, 255, 0.15); color: ${awayColor}; }
        .details-table {
          width: 100%;
          border-collapse: collapse;
          margin-bottom: 20px;
        }
        .details-table td {
          padding: 10px;
          border-bottom: 1px solid var(--divider-color);
        }
        .details-table td.label-col {
          font-weight: bold;
          color: var(--secondary-text-color);
          width: 35%;
        }
        .btn-ha-link {
          display: inline-block;
          background: var(--primary-color);
          color: var(--text-primary-color);
          padding: 8px 16px;
          border-radius: 4px;
          text-decoration: none;
          font-weight: 500;
          font-size: 0.9rem;
          transition: opacity 0.2s;
          text-align: center;
          width: 100%;
          box-sizing: border-box;
        }
        .btn-ha-link:hover {
          opacity: 0.9;
        }
        .btn-exclude {
          color: white;
          border: none;
          border-radius: 4px;
          padding: 6px 10px;
          font-size: 0.8rem;
          cursor: pointer;
          font-weight: 500;
          transition: opacity 0.2s;
        }
        .btn-exclude:hover { opacity: 0.9; }
        .btn-exclude[disabled] { opacity: 0.4; cursor: not-allowed; }
      </style>
      <div class="details-container">
        <div class="details-header">
          <div class="details-title">${name}</div>
          <span class="details-badge ${stateClass}">${stateLabel}</span>
        </div>
        <table class="details-table">
          <tr>
            <td class="label-col">MAC Address</td>
            <td><code>${mac}</code></td>
          </tr>
          <tr>
            <td class="label-col">IP Address</td>
            <td>
              <div style="display: flex; align-items: center; justify-content: space-between;">
                <span>${ip}</span>
                ${ip !== 'Unknown' ? `<button id="btn-ping" style="background: var(--primary-color); color: var(--text-primary-color); border: none; border-radius: 4px; padding: 2px 8px; font-size: 0.75rem; cursor: pointer;">Ping</button>` : ''}
              </div>
              <div id="ping-result" style="font-size: 0.8rem; color: var(--secondary-text-color); margin-top: 4px; display: none;"></div>
            </td>
          </tr>
          <tr>
            <td class="label-col">Device Type</td>
            <td>${kind}</td>
          </tr>
          <tr>
            <td class="label-col">Connection Port</td>
            <td>${parentName} - ${portName}</td>
          </tr>
          <tr>
            <td class="label-col">Linked Integrations</td>
            <td>${linkedIntegrationsHtml}</td>
          </tr>
          <tr>
            <td class="label-col">Current Duration</td>
            <td><strong>${durationStr}</strong> (Since ${history.length > 0 ? formatTime(history[history.length - 1].timestamp) : 'unknown'})</td>
          </tr>
          <tr>
            <td class="label-col">Last Seen</td>
            <td>${lastSeenStr}</td>
          </tr>
        </table>
        ${historyHtml}
        
        <div style="margin-top: 15px; border-top: 1px solid var(--divider-color); padding-top: 12px;">
          <div style="font-weight: bold; font-size: 0.9rem; color: var(--secondary-text-color); margin-bottom: 8px;">Exclusion Controls</div>
          <div style="display: flex; gap: 8px; flex-wrap: wrap;">
            <button id="btn-exclude-mac" class="btn-exclude" style="background: var(--error-color, #ff5252); flex: 1; min-width: 90px;">Exclude MAC</button>
            <button id="btn-exclude-ip" class="btn-exclude" style="background: var(--error-color, #ff5252); flex: 1; min-width: 90px;" ${ip === 'Unknown' ? 'disabled' : ''}>Exclude IP</button>
            <button id="btn-exclude-name" class="btn-exclude" style="background: var(--error-color, #ff5252); flex: 1; min-width: 90px;" ${name === 'Unknown' ? 'disabled' : ''}>Exclude Name</button>
          </div>
        </div>

        <div style="margin-top: 15px; border-top: 1px solid var(--divider-color); padding-top: 12px;">
          <div style="font-weight: bold; font-size: 0.9rem; color: var(--secondary-text-color); margin-bottom: 8px;">Link Home Assistant Device</div>
          <div style="display: flex; gap: 8px; align-items: center;">
            <select id="select-ha-device" style="flex: 1; padding: 6px; border-radius: 4px; border: 1px solid var(--divider-color); background: var(--card-background-color, #1c1c1c); color: var(--primary-text-color); font-size: 0.85rem;">
              <option value="">-- Autodetect or Unlinked --</option>
              ${Object.values(this._hass?.devices || {})
                .filter(d => {
                  if (d.id === deviceId) return true;
                  
                  // Exclude devices that only belong to SNMP integrations.
                  const hasOtherIntegration = d.identifiers && d.identifiers.some(id => 
                    id[0] !== 'snmp_network_manager' && id[0] !== 'snmp_switch_manager'
                  );
                  return hasOtherIntegration;
                })
                .sort((a, b) => {
                  const nameA = a.name_by_user || a.name || '';
                  const nameB = b.name_by_user || b.name || '';
                  return nameA.localeCompare(nameB);
                }).map(d => {
                  const dName = d.name_by_user || d.name || d.id;
                  const info = [d.manufacturer, d.model].filter(Boolean).join(' ');
                  const label = info ? `${dName} (${info})` : dName;
                  return `<option value="${d.id}" ${d.id === deviceId ? 'selected' : ''}>${label}</option>`;
                }).join('')}
            </select>
            <button id="btn-save-link" style="background: var(--primary-color); color: var(--text-primary-color); border: none; border-radius: 4px; padding: 6px 12px; font-size: 0.8rem; cursor: pointer; font-weight: 500;">Save</button>
          </div>
        </div>

        <div style="margin-top: 20px;">
          ${deviceId ? `
            <a href="/config/devices/device/${deviceId}" target="_blank" class="btn-ha-link">
              Open Home Assistant Device Page
            </a>
          ` : `
            <div style="font-size: 0.85rem; color: var(--secondary-text-color); font-style: italic; text-align: center;">
              This device has not been linked to a Home Assistant Device registry entry yet.
            </div>
          `}
        </div>
      </div>
    `;

    // Exclude button handlers
    const excludeMacBtn = this.shadowRoot.getElementById('btn-exclude-mac');
    if (excludeMacBtn) {
      excludeMacBtn.onclick = async () => {
        if (!confirm(`Are you sure you want to exclude MAC address ${mac} from the network map?`)) return;
        try {
          await this._hass.callWS({
            type: 'snmp_network_manager/exclude_device',
            exclude_type: 'mac',
            value: mac
          });
          modal.style.display = 'none';
        } catch (e) {
          alert(`Failed to exclude MAC: ${e.message}`);
        }
      };
    }
    
    const excludeIpBtn = this.shadowRoot.getElementById('btn-exclude-ip');
    if (excludeIpBtn && ip !== 'Unknown') {
      excludeIpBtn.onclick = async () => {
        if (!confirm(`Are you sure you want to exclude IP address ${ip} from the network map?`)) return;
        try {
          await this._hass.callWS({
            type: 'snmp_network_manager/exclude_device',
            exclude_type: 'ip',
            value: ip
          });
          modal.style.display = 'none';
        } catch (e) {
          alert(`Failed to exclude IP: ${e.message}`);
        }
      };
    }
    
    const excludeNameBtn = this.shadowRoot.getElementById('btn-exclude-name');
    if (excludeNameBtn && name !== 'Unknown') {
      excludeNameBtn.onclick = async () => {
        if (!confirm(`Are you sure you want to exclude device name "${name}" from the network map?`)) return;
        try {
          await this._hass.callWS({
            type: 'snmp_network_manager/exclude_device',
            exclude_type: 'name',
            value: name
          });
          modal.style.display = 'none';
        } catch (e) {
          alert(`Failed to exclude name: ${e.message}`);
        }
      };
    }

    // Ping button handler
    const pingBtn = this.shadowRoot.getElementById('btn-ping');
    const pingRes = this.shadowRoot.getElementById('ping-result');
    if (pingBtn && pingRes) {
      pingBtn.onclick = async () => {
        pingBtn.disabled = true;
        pingRes.style.display = 'block';
        pingRes.innerHTML = '<span style="color: var(--primary-color);">Pinging...</span>';
        try {
          const res = await this._hass.callWS({
            type: 'snmp_network_manager/ping_device',
            ip: ip
          });
          const latency = res.latency;
          if (latency === 'Timeout') {
            pingRes.innerHTML = '<span style="color: var(--error-color, #ff5252);">Ping Timeout (Offline)</span>';
          } else if (latency === 'Failed') {
            pingRes.innerHTML = '<span style="color: var(--error-color, #ff5252);">Ping Execution Failed</span>';
          } else {
            pingRes.innerHTML = `Ping Success: <strong style="color: #00e676;">${latency}</strong>`;
          }
        } catch (e) {
          pingRes.innerHTML = `<span style="color: var(--error-color, #ff5252);">Error: ${e.message}</span>`;
        } finally {
          pingBtn.disabled = false;
        }
      };
    }

    // Manual linker handler
    const saveLinkBtn = this.shadowRoot.getElementById('btn-save-link');
    const selectDevice = this.shadowRoot.getElementById('select-ha-device');
    if (saveLinkBtn && selectDevice) {
      saveLinkBtn.onclick = async () => {
        const selectedId = selectDevice.value;
        saveLinkBtn.disabled = true;
        try {
          await this._hass.callWS({
            type: 'snmp_network_manager/link_device',
            mac: mac,
            device_id: selectedId || null
          });
          modal.style.display = 'none';
          
          // Wait a short moment for reload and force refresh the graph
          setTimeout(() => {
            console.log("[SNMP Network Map] Forcing graph fetch after manual link update");
            this._fetchGraphData();
          }, 1000);
        } catch (e) {
          alert(`Failed to link device: ${e.message}`);
        } finally {
          saveLinkBtn.disabled = false;
        }
      };
    }

    const closeBtn = this.shadowRoot.querySelector('.close');
    closeBtn.onclick = () => { modal.style.display = 'none'; body.innerHTML = ''; };
    modal.onclick = (e) => { if (e.target === modal) { modal.style.display = 'none'; body.innerHTML = ''; } };
  }

  _renderGraph() {
    console.log("[SNMP Network Map] _renderGraph() called. this._nodes:", this._nodes, "view:", this._cardView);
    if (!this._nodes) {
      console.warn("[SNMP Network Map] _renderGraph: this._nodes is null or undefined!");
      return;
    }
    
    const asleepColor = this._config.asleep_color || '#ffb300';
    const awayColor = this._config.away_color || '#b388ff';
    const showUnlinked = this._config.show_unlinked_indicator !== false;
    
    // Toggle container display and Map counts
    const wrapperEl = this.shadowRoot.getElementById('map-wrapper');
    const listEl = this.shadowRoot.getElementById('list-container');
    const groupedEl = this.shadowRoot.getElementById('grouped-container');
    const mapSummaryBar = this.shadowRoot.getElementById('map-summary-bar');
    
    if (wrapperEl) wrapperEl.style.display = this._cardView === 'map' ? 'block' : 'none';
    if (listEl) listEl.style.display = this._cardView === 'list' ? 'block' : 'none';
    if (groupedEl) groupedEl.style.display = this._cardView === 'grouped' ? 'block' : 'none';
    
    if (mapSummaryBar) {
      if (this._cardView === 'map') {
        const gateways = Object.entries(this._nodes).filter(([id, n]) => {
          const name = (n.attrs?.name || '').toLowerCase();
          return n.kind === 'device' && (n.attrs?.is_gateway || name.includes('gateway') || name.includes('router') || name.includes('firewall'));
        });
        const switches = Object.entries(this._nodes).filter(([id, n]) => {
          const name = (n.attrs?.name || '').toLowerCase();
          const isGateway = n.attrs?.is_gateway || name.includes('gateway') || name.includes('router') || name.includes('firewall');
          const isAP = n.attrs?.is_ap || name.startsWith('ap ') || name.startsWith('ap-') || name === 'ap' || id.includes(':ap:');
          return n.kind === 'device' && n.attrs?.integration === 'snmp_switch_manager' && !isGateway && !isAP;
        });
        const caps = Object.entries(this._nodes).filter(([id, n]) => {
          const name = (n.attrs?.name || '').toLowerCase();
          return n.kind === 'device' && (n.attrs?.is_ap || name.startsWith('ap ') || name.startsWith('ap-') || name === 'ap' || id.includes(':ap:'));
        });
        const endpoints = Object.entries(this._nodes).filter(([id, n]) => n.kind === 'endpoint');
        
        let onlineCount = 0;
        let offlineCount = 0;
        let asleepCount = 0;
        let awayCount = 0;
        Object.values(this._nodes).forEach(n => {
          const state = n.attrs?.state || 'up';
          if (state === 'up') onlineCount++;
          else if (state === 'down') offlineCount++;
          else if (state === 'asleep') asleepCount++;
          else if (state === 'away') awayCount++;
        });

        mapSummaryBar.innerHTML = `
          <div class="summary-item">
            <ha-icon icon="mdi:shield-key" class="summary-icon"></ha-icon>
            <span><strong>Gateways:</strong> ${gateways.length}</span>
          </div>
          <div class="summary-item">
            <ha-icon icon="mdi:router-network" class="summary-icon"></ha-icon>
            <span><strong>Switches:</strong> ${switches.length}</span>
          </div>
          <div class="summary-item">
            <ha-icon icon="mdi:access-point" class="summary-icon"></ha-icon>
            <span><strong>APs:</strong> ${caps.length}</span>
          </div>
          <div class="summary-item">
            <ha-icon icon="mdi:lan-connect" class="summary-icon"></ha-icon>
            <span><strong>Endpoints:</strong> ${endpoints.length}</span>
          </div>
          <div class="summary-separator" style="width: 1px; background: var(--divider-color); margin: 0 4px; align-self: stretch;"></div>
          <div class="summary-item">
            <ha-icon icon="mdi:check-circle" class="summary-icon" style="color: #00e676;"></ha-icon>
            <span><strong>Online:</strong> ${onlineCount}</span>
          </div>
          <div class="summary-item">
            <ha-icon icon="mdi:close-circle" class="summary-icon" style="color: #ff0000;"></ha-icon>
            <span><strong>Offline:</strong> ${offlineCount}</span>
          </div>
          <div class="summary-item">
            <ha-icon icon="mdi:power-sleep" class="summary-icon" style="color: ${asleepColor};"></ha-icon>
            <span><strong>Asleep:</strong> ${asleepCount}</span>
          </div>
          <div class="summary-item">
            <ha-icon icon="mdi:home-export-outline" class="summary-icon" style="color: ${awayColor};"></ha-icon>
            <span><strong>Away:</strong> ${awayCount}</span>
          </div>
        `;
        mapSummaryBar.style.display = 'flex';
      } else {
        mapSummaryBar.style.display = 'none';
      }
    }

    // Show/hide layout buttons
    const layoutControls = this.shadowRoot.querySelectorAll('.layout-control');
    if (layoutControls) {
      layoutControls.forEach(el => {
        if (el.id === 'btn-cancel') {
          el.style.display = (this._cardView === 'map' && this._hasMovedNodes) ? 'inline-block' : 'none';
        } else if (el.id === 'btn-delete') {
          const isCustom = !['clustered', 'concentric', 'force', 'grid'].includes(this._layoutMode);
          el.style.display = (this._cardView === 'map' && isCustom) ? 'inline-block' : 'none';
        } else {
          el.style.display = this._cardView === 'map' ? 'inline-block' : 'none';
        }
      });
    }

    if (this._cardView === 'list') {
      if (listEl) {
        if (Object.keys(this._nodes || {}).length === 0) {
          listEl.innerHTML = '<div class="no-devices" style="color: var(--secondary-text-color); font-style: italic; padding: 10px;">No discovered devices found.</div>';
          return;
        }

        const allNodes = Object.entries(this._nodes).map(([id, node]) => {
          let type = 'Client';
          let icon = 'mdi:lan-connect';
          if (node.kind === 'device') {
            const name = (node.attrs?.name || '').toLowerCase();
            const isGateway = node.attrs?.is_gateway || name.includes('gateway') || name.includes('router') || name.includes('firewall');
            const isAP = node.attrs?.is_ap || name.startsWith('ap ') || name.startsWith('ap-') || name === 'ap' || id.includes(':ap:');
            if (isGateway) {
              type = 'Gateway';
              icon = 'mdi:shield-key';
            } else if (isAP) {
              type = 'Access Point';
              icon = 'mdi:access-point';
            } else if (node.attrs?.integration === 'snmp_switch_manager') {
              type = 'Switch';
              icon = 'mdi:router-network';
            } else {
              type = 'Device';
              icon = 'mdi:help-network';
            }
          }
          
          const mac = node.attrs?.mac || node.attrs?.sysPhysAddr || '';
          const ip = node.attrs?.ip || node.attrs?.host || 'Unknown';
          const name = node.attrs?.name || mac || id;
          const isDown = node.attrs?.state === 'down';
          const isAsleep = node.attrs?.state === 'asleep';
          const isAway = node.attrs?.state === 'away';
          
          let parentName = 'Unknown';
          let portName = 'Unknown';
          
          this._edges.forEach(edge => {
            if (edge.src === edge.dst) return; // Skip self-loops
            if (edge.dst === id && edge.kind === 'lldp') {
              const parent = this._nodes[edge.src];
              if (parent) parentName = parent.attrs?.name || 'Unknown';
              portName = edge.attrs?.local_port || edge.attrs?.port_name || edge.attrs?.bridge_port || 'Unknown';
            } else if (edge.src === id && edge.kind === 'fdb') {
              const parent = this._nodes[edge.dst];
              if (parent) parentName = parent.attrs?.name || 'Unknown';
              portName = edge.attrs?.bridge_port || 'Unknown';
            }
          });
          
          return {
            id,
            node,
            name,
            mac,
            ip,
            type,
            icon,
            parentName,
            portName,
            isDown,
            isAsleep,
            isAway
          };
        });

        // Search Filter
        const query = (this._searchQuery || '').toLowerCase();
        const filteredNodes = allNodes.filter(item => {
          return (
            item.name.toLowerCase().includes(query) ||
            item.mac.toLowerCase().includes(query) ||
            item.ip.toLowerCase().includes(query) ||
            item.type.toLowerCase().includes(query) ||
            item.parentName.toLowerCase().includes(query) ||
            item.portName.toLowerCase().includes(query)
          );
        });

        // Dynamic summaries based on filtered list
        const gatewaysCount = filteredNodes.filter(item => item.type === 'Gateway').length;
        const switchesCount = filteredNodes.filter(item => item.type === 'Switch').length;
        const apsCount = filteredNodes.filter(item => item.type === 'Access Point').length;
        const endpointsCount = filteredNodes.filter(item => item.type === 'Client').length;

        // Sort
        filteredNodes.sort((a, b) => {
          let valA = a[this._sortKey];
          let valB = b[this._sortKey];
          
          if (this._sortKey === 'isDown') {
            valA = valA ? 1 : 0;
            valB = valB ? 1 : 0;
          }
          
          if (typeof valA === 'string') {
            return this._sortOrder === 'asc' 
              ? valA.localeCompare(valB, undefined, { numeric: true, sensitivity: 'base' })
              : valB.localeCompare(valA, undefined, { numeric: true, sensitivity: 'base' });
          } else {
            return this._sortOrder === 'asc' ? valA - valB : valB - valA;
          }
        });

          let onlineCount = 0;
          let offlineCount = 0;
          let asleepCount = 0;
          let awayCount = 0;
          filteredNodes.forEach(item => {
            if (item.isDown) offlineCount++;
            else if (item.isAsleep) asleepCount++;
            else if (item.isAway) awayCount++;
            else onlineCount++;
          });

          listEl.innerHTML = `
          <style>
            .search-bar { margin-bottom: 12px; }
            .search-bar input { width: 100%; max-width: 300px; padding: 8px 12px; border-radius: 4px; border: 1px solid var(--divider-color); background: rgba(0,0,0,0.2); color: var(--primary-text-color); }
            .device-table { width: 100%; border-collapse: collapse; margin-top: 8px; }
            .device-table th, .device-table td { padding: 10px; text-align: left; border-bottom: 1px solid var(--divider-color); }
            .device-table th { font-weight: bold; color: var(--secondary-text-color); }
            .device-table th.sort-header { cursor: pointer; user-select: none; }
            .device-table th.sort-header:hover { color: var(--primary-text-color); }
            .device-row { cursor: pointer; transition: background 0.2s; }
            .device-row:hover { background: rgba(255,255,255,0.05); }
            .device-name { font-weight: 500; display: flex; align-items: center; gap: 8px; }
            .device-icon { color: var(--primary-color); }
            .device-icon.down { color: var(--error-color); opacity: 0.6; }
            .device-icon.asleep { color: ${asleepColor}; opacity: 0.8; }
            .device-icon.away { color: ${awayColor}; opacity: 0.8; }
            .state-badge { padding: 3px 8px; border-radius: 12px; font-size: 0.75rem; font-weight: bold; }
            .state-badge.online { background: rgba(0, 230, 118, 0.15); color: #00e676; }
            .state-badge.offline { background: rgba(255, 0, 0, 0.15); color: #ff0000; }
            .state-badge.asleep { background: rgba(255, 179, 0, 0.15); color: ${asleepColor}; }
            .state-badge.away { background: rgba(179, 136, 255, 0.15); color: ${awayColor}; }
          </style>
          <div class="summary-bar">
            <div class="summary-item">
              <ha-icon icon="mdi:shield-key" class="summary-icon"></ha-icon>
              <span><strong>Gateways:</strong> ${gatewaysCount}</span>
            </div>
            <div class="summary-item">
              <ha-icon icon="mdi:router-network" class="summary-icon"></ha-icon>
              <span><strong>Switches:</strong> ${switchesCount}</span>
            </div>
            <div class="summary-item">
              <ha-icon icon="mdi:access-point" class="summary-icon"></ha-icon>
              <span><strong>APs:</strong> ${apsCount}</span>
            </div>
            <div class="summary-item">
              <ha-icon icon="mdi:lan-connect" class="summary-icon"></ha-icon>
              <span><strong>Endpoints:</strong> ${endpointsCount}</span>
            </div>
            <div class="summary-separator" style="width: 1px; background: var(--divider-color); margin: 0 4px; align-self: stretch;"></div>
            <div class="summary-item">
              <ha-icon icon="mdi:check-circle" class="summary-icon" style="color: #00e676;"></ha-icon>
              <span><strong>Online:</strong> ${onlineCount}</span>
            </div>
            <div class="summary-item">
              <ha-icon icon="mdi:close-circle" class="summary-icon" style="color: #ff0000;"></ha-icon>
              <span><strong>Offline:</strong> ${offlineCount}</span>
            </div>
            <div class="summary-item">
              <ha-icon icon="mdi:power-sleep" class="summary-icon" style="color: ${asleepColor};"></ha-icon>
              <span><strong>Asleep:</strong> ${asleepCount}</span>
            </div>
            <div class="summary-item">
              <ha-icon icon="mdi:home-export-outline" class="summary-icon" style="color: ${awayColor};"></ha-icon>
              <span><strong>Away:</strong> ${awayCount}</span>
            </div>
          </div>
          
          <div class="search-bar">
            <input type="text" id="list-search" placeholder="Search devices..." value="${this._searchQuery || ''}" />
          </div>
          
          <table class="device-table">
            <thead>
              <tr>
                <th class="sort-header" data-key="name">Device ${this._sortKey === 'name' ? (this._sortOrder === 'asc' ? ' ▲' : ' ▼') : ''}</th>
                <th class="sort-header" data-key="type">Type ${this._sortKey === 'type' ? (this._sortOrder === 'asc' ? ' ▲' : ' ▼') : ''}</th>
                <th class="sort-header" data-key="mac">MAC Address ${this._sortKey === 'mac' ? (this._sortOrder === 'asc' ? ' ▲' : ' ▼') : ''}</th>
                <th class="sort-header" data-key="ip">IP Address ${this._sortKey === 'ip' ? (this._sortOrder === 'asc' ? ' ▲' : ' ▼') : ''}</th>
                <th class="sort-header" data-key="parentName">Connected Switch / Port ${this._sortKey === 'parentName' ? (this._sortOrder === 'asc' ? ' ▲' : ' ▼') : ''}</th>
                <th class="sort-header" data-key="isDown">Status ${this._sortKey === 'isDown' ? (this._sortOrder === 'asc' ? ' ▲' : ' ▼') : ''}</th>
              </tr>
            </thead>
            <tbody>
              ${filteredNodes.map(item => {
                const isUnlinked = !item.node?.attrs?.device_id;
                const unlinkedHtml = (showUnlinked && isUnlinked)
                  ? `<ha-icon icon="mdi:link-off" title="Unlinked from Home Assistant Device" style="--mdc-icon-size: 14px; color: var(--error-color, #ff5252); margin-left: 4px; opacity: 0.8; vertical-align: middle;"></ha-icon>`
                  : '';
                return `
                  <tr class="device-row" data-id="${item.id}">
                    <td class="device-name">
                      <ha-icon icon="${item.icon}" class="device-icon ${item.isDown ? 'down' : (item.isAsleep ? 'asleep' : (item.isAway ? 'away' : ''))}"></ha-icon>
                      <span>${item.name}</span>${unlinkedHtml}
                    </td>
                    <td>${item.type}</td>
                    <td><code>${item.mac}</code></td>
                    <td>${item.ip}</td>
                    <td>${item.parentName} - ${item.portName}</td>
                    <td>
                      <span class="state-badge ${item.isDown ? 'offline' : (item.isAsleep ? 'asleep' : (item.isAway ? 'away' : 'online'))}">
                        ${item.isDown ? 'Offline' : (item.isAsleep ? 'Asleep' : (item.isAway ? 'Away' : 'Online'))}
                      </span>
                    </td>
                  </tr>
                `;
              }).join('')}
            </tbody>
          </table>
        `;
        
        const searchInput = listEl.querySelector('#list-search');
        if (searchInput) {
          searchInput.oninput = (e) => {
            this._searchQuery = e.target.value;
            this._renderGraph();
          };
          
          if (this.shadowRoot.activeElement === searchInput || this._searchQuery) {
            searchInput.focus();
            const val = searchInput.value;
            searchInput.value = '';
            searchInput.value = val;
          }
        }

        listEl.querySelectorAll('.sort-header').forEach(header => {
          header.onclick = () => {
            const key = header.getAttribute('data-key');
            if (this._sortKey === key) {
              this._sortOrder = this._sortOrder === 'asc' ? 'desc' : 'asc';
            } else {
              this._sortKey = key;
              this._sortOrder = 'asc';
            }
            this._renderGraph();
          };
        });

        listEl.querySelectorAll('.device-row').forEach(row => {
          row.onclick = () => {
            try {
              const id = row.getAttribute('data-id');
              const node = this._nodes[id];
              if (node) {
                const name = (node.attrs?.name || '').toLowerCase();
                const isAP = node.attrs?.is_ap || name.startsWith('ap ') || name.startsWith('ap-') || name === 'ap' || id.includes(':ap:');
                if (node.attrs?.integration === 'snmp_switch_manager' && !isAP) {
                  this._openSwitchCard(node, id);
                } else {
                  this._openDeviceDetailsCard(node, id);
                }
              }
            } catch (err) {
              alert("List Row Click Error: " + err.message + "\n" + err.stack);
              console.error(err);
            }
          };
        });
      }
      return;
    }

    if (this._cardView === 'grouped') {
      if (groupedEl) {
        if (Object.keys(this._nodes || {}).length === 0) {
          groupedEl.innerHTML = '<div class="no-devices" style="color: var(--secondary-text-color); font-style: italic; padding: 10px;">No discovered devices found.</div>';
          return;
        }

        // Programmatically identify all parent groups by scanning the edges
        const parentMap = new Map();
        
        // Pre-initialize all kind === 'device' nodes as potential parent groups
        Object.entries(this._nodes).forEach(([id, n]) => {
          if (n.kind === 'device') {
            parentMap.set(id, []);
          }
        });

        this._edges.forEach(edge => {
          let parentId = null;
          let childId = null;
          let port = 'Unknown';
          
          if (edge.kind === 'lldp') {
            parentId = edge.src;
            childId = edge.dst;
            port = edge.attrs?.local_port || edge.attrs?.port_name || 'Link';
          } else if (edge.kind === 'fdb') {
            parentId = edge.dst;
            childId = edge.src;
            port = edge.attrs?.bridge_port || 'Unknown';
          }
          
          if (parentId && childId && parentMap.has(parentId)) {
            if (parentId === childId) {
              return; // Skip self-loops
            }
            const childNode = this._nodes[childId];
            if (childNode) {
              const existing = parentMap.get(parentId);
              if (!existing.some(item => item.id === childId)) {
                existing.push({
                  id: childId,
                  node: childNode,
                  port: port
                });
              }
            }
          }
        });

        // Filter out parent groups that are endpoints or are not in _nodes
        const sortedParents = Array.from(parentMap.entries()).filter(([pId, _]) => this._nodes[pId]);
        
        // Sort parent groups alphabetically by name
        sortedParents.sort((a, b) => {
          const nameA = this._nodes[a[0]].attrs?.name || a[0];
          const nameB = this._nodes[b[0]].attrs?.name || b[0];
          return nameA.localeCompare(nameB, undefined, { numeric: true, sensitivity: 'base' });
        });

        // Sort children inside each group by port name
        sortedParents.forEach(([_, children]) => {
          children.sort((a, b) => a.port.localeCompare(b.port, undefined, { numeric: true }));
        });

        const query = (this._searchQuery || '').toLowerCase();
        
        const displayedGateways = new Set();
        const displayedSwitches = new Set();
        const displayedAPs = new Set();
        const displayedEndpoints = new Set();
        
        const filteredGroups = [];
        
        sortedParents.forEach(([parentId, children]) => {
          const parentNode = this._nodes[parentId];
          const parentName = (parentNode.attrs?.name || parentId).toLowerCase();
          const parentIp = (parentNode.attrs?.ip || '').toLowerCase();
          
          const matchingChildren = children.filter(item => {
            const childName = (item.node.attrs?.name || '').toLowerCase();
            const childMac = (item.node.attrs?.mac || '').toLowerCase();
            const childIp = (item.node.attrs?.ip || '').toLowerCase();
            const childPort = (item.port || '').toLowerCase();
            return childName.includes(query) || childMac.includes(query) || childIp.includes(query) || childPort.includes(query);
          });
          
          const parentMatches = parentName.includes(query) || parentIp.includes(query);
          
          if (parentMatches || matchingChildren.length > 0) {
            const childrenToShow = parentMatches ? children : matchingChildren;
            filteredGroups.push({
              parentId,
              parentNode,
              children: childrenToShow
            });
            
            // Track unique displayed devices
            const pName = (parentNode.attrs?.name || '').toLowerCase();
            const isGateway = parentNode.attrs?.is_gateway || pName.includes('gateway') || pName.includes('router') || pName.includes('firewall');
            const isAP = parentNode.attrs?.is_ap || pName.startsWith('ap ') || pName.startsWith('ap-') || pName === 'ap' || parentId.includes(':ap:');
            
            if (isGateway) {
              displayedGateways.add(parentId);
            } else if (isAP) {
              displayedAPs.add(parentId);
            } else if (parentNode.attrs?.integration === 'snmp_switch_manager') {
              displayedSwitches.add(parentId);
            }
            
            childrenToShow.forEach(item => {
              const cName = (item.node.attrs?.name || '').toLowerCase();
              const cIsGateway = item.node.attrs?.is_gateway || cName.includes('gateway') || cName.includes('router') || cName.includes('firewall');
              const cIsAP = item.node.attrs?.is_ap || cName.startsWith('ap ') || cName.startsWith('ap-') || cName === 'ap' || item.id.includes(':ap:');
              
              if (item.node.kind === 'endpoint') {
                displayedEndpoints.add(item.id);
              } else if (cIsGateway) {
                displayedGateways.add(item.id);
              } else if (cIsAP) {
                displayedAPs.add(item.id);
              } else if (item.node.attrs?.integration === 'snmp_switch_manager') {
                displayedSwitches.add(item.id);
              }
            });
          }
        });

        const totalGateways = displayedGateways.size;
        const totalSwitches = displayedSwitches.size;
        const totalAPs = displayedAPs.size;
        const totalEndpoints = displayedEndpoints.size;

        const allDisplayedIds = new Set([
          ...displayedGateways,
          ...displayedSwitches,
          ...displayedAPs,
          ...displayedEndpoints
        ]);
        let onlineCount = 0;
        let offlineCount = 0;
        let asleepCount = 0;
        let awayCount = 0;
        allDisplayedIds.forEach(nid => {
          const node = this._nodes[nid];
          if (node) {
            const state = node.attrs?.state || 'up';
            if (state === 'up') onlineCount++;
            else if (state === 'down') offlineCount++;
            else if (state === 'asleep') asleepCount++;
            else if (state === 'away') awayCount++;
          }
        });

        groupedEl.innerHTML = `
          <style>
            .switch-group { margin-bottom: 28px; }
            .switch-header { display: flex; align-items: center; gap: 8px; font-size: 1.1rem; font-weight: bold; border-bottom: 2px solid var(--primary-color); padding-bottom: 8px; margin-bottom: 12px; cursor: pointer; transition: color 0.2s; }
            .switch-header:hover { color: var(--primary-color); }
            .switch-icon { color: var(--primary-color); }
            .port-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
            .port-card { background: rgba(255,255,255,0.03); border: 1px solid var(--divider-color); border-radius: 6px; padding: 10px; display: flex; align-items: center; justify-content: space-between; cursor: pointer; transition: all 0.2s; }
            .port-card:hover { background: rgba(255,255,255,0.07); border-color: var(--primary-color); }
            .port-info { display: flex; flex-direction: column; gap: 2px; }
            .port-num { font-size: 0.75rem; color: var(--secondary-text-color); font-weight: bold; }
            .dev-name { font-weight: 500; font-size: 0.9rem; }
            .dev-ip { font-size: 0.75rem; color: var(--secondary-text-color); }
            .device-icon.down { color: var(--error-color); opacity: 0.6; }
            .device-icon.asleep { color: ${asleepColor}; opacity: 0.8; }
            .device-icon.away { color: ${awayColor}; opacity: 0.8; }
            .search-bar { margin-bottom: 12px; }
            .search-bar input { width: 100%; max-width: 300px; padding: 8px 12px; border-radius: 4px; border: 1px solid var(--divider-color); background: rgba(0,0,0,0.2); color: var(--primary-text-color); }
          </style>
          
          <div class="summary-bar">
            <div class="summary-item">
              <ha-icon icon="mdi:shield-key" class="summary-icon"></ha-icon>
              <span><strong>Gateways:</strong> ${totalGateways}</span>
            </div>
            <div class="summary-item">
              <ha-icon icon="mdi:router-network" class="summary-icon"></ha-icon>
              <span><strong>Switches:</strong> ${totalSwitches}</span>
            </div>
            <div class="summary-item">
              <ha-icon icon="mdi:access-point" class="summary-icon"></ha-icon>
              <span><strong>APs:</strong> ${totalAPs}</span>
            </div>
            <div class="summary-item">
              <ha-icon icon="mdi:lan-connect" class="summary-icon"></ha-icon>
              <span><strong>Endpoints:</strong> ${totalEndpoints}</span>
            </div>
            <div class="summary-separator" style="width: 1px; background: var(--divider-color); margin: 0 4px; align-self: stretch;"></div>
            <div class="summary-item">
              <ha-icon icon="mdi:check-circle" class="summary-icon" style="color: #00e676;"></ha-icon>
              <span><strong>Online:</strong> ${onlineCount}</span>
            </div>
            <div class="summary-item">
              <ha-icon icon="mdi:close-circle" class="summary-icon" style="color: #ff0000;"></ha-icon>
              <span><strong>Offline:</strong> ${offlineCount}</span>
            </div>
            <div class="summary-item">
              <ha-icon icon="mdi:power-sleep" class="summary-icon" style="color: ${asleepColor};"></ha-icon>
              <span><strong>Asleep:</strong> ${asleepCount}</span>
            </div>
            <div class="summary-item">
              <ha-icon icon="mdi:home-export-outline" class="summary-icon" style="color: ${awayColor};"></ha-icon>
              <span><strong>Away:</strong> ${awayCount}</span>
            </div>
          </div>

          <div class="search-bar">
            <input type="text" id="grouped-search" placeholder="Search devices..." value="${this._searchQuery || ''}" />
          </div>
 
          ${filteredGroups.map(({ parentId, parentNode, children }) => {
            let icon = 'mdi:help-network';
            const pName = (parentNode.attrs?.name || '').toLowerCase();
            const isGateway = parentNode.attrs?.is_gateway || pName.includes('gateway') || pName.includes('router') || pName.includes('firewall');
            if (isGateway) {
              icon = 'mdi:shield-key';
            } else if (parentNode.attrs?.is_ap) {
              icon = 'mdi:access-point';
            } else if (parentNode.attrs?.integration === 'snmp_switch_manager') {
              icon = 'mdi:router-network';
            }
            
            return `
              <div class="switch-group" data-id="${parentId}">
                <div class="switch-header">
                  <ha-icon icon="${icon}" class="switch-icon"></ha-icon>
                  <span>${parentNode.attrs?.name || parentId}</span>
                  <span style="font-size: 0.8rem; font-weight: normal; color: var(--secondary-text-color); margin-left: 8px;">
                    (${children.length} connected)
                  </span>
                </div>
                ${children.length === 0 ? 
                  '<div class="no-devices" style="color: var(--secondary-text-color); font-style: italic; padding: 10px 0 10px 20px;">No devices connected.</div>' :
                  `
                  <div class="port-grid">
                    ${children.map(item => {
                      const mac = item.node.attrs?.mac || '';
                      const ip = item.node.attrs?.ip || 'Unknown';
                      const rawName = item.node.attrs?.name || mac;
                      const name = (rawName.includes('-') || rawName.includes('_') || rawName === rawName.toLowerCase()) && !/^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$/.test(rawName) && !/^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(rawName)
                        ? rawName.replace(/[-_]/g, ' ').replace(/\b\w/g, c => c.toUpperCase()).replace(/([A-Za-z])'S\b/g, "$1's")
                        : rawName;
                      const state = item.node.attrs?.state || 'down';
                      const childIcon = item.node.attrs?.is_ap ? 'mdi:access-point' : 'mdi:lan-connect';
                      
                      let iconColor = '#00e676';
                      if (state === 'down') iconColor = 'var(--error-color)';
                      else if (state === 'asleep') iconColor = asleepColor;
                      else if (state === 'away') iconColor = awayColor;
 
                      const isUnlinked = !item.node?.attrs?.device_id;
                      const unlinkedHtml = (showUnlinked && isUnlinked)
                        ? `<ha-icon icon="mdi:link-off" title="Unlinked from Home Assistant Device" style="--mdc-icon-size: 13px; color: var(--error-color, #ff5252); margin-left: 4px; opacity: 0.8; vertical-align: middle;"></ha-icon>`
                        : '';
                      return `
                        <div class="port-card" data-id="${item.id}">
                          <div class="port-info">
                            <span class="port-num">${item.port}</span>
                            <span class="dev-name">${name}${unlinkedHtml}</span>
                            <span class="dev-ip">${ip}</span>
                          </div>
                          <ha-icon icon="${childIcon}" style="color: ${iconColor};" class="device-icon ${state === 'down' ? 'down' : (state === 'asleep' ? 'asleep' : (state === 'away' ? 'away' : ''))}"></ha-icon>
                        </div>
                      `;
                    }).join('')}
                  </div>
                  `
                }
              </div>
            `;
          }).join('')}
        `;
 
        const searchInput = groupedEl.querySelector('#grouped-search');
        if (searchInput) {
          searchInput.oninput = (e) => {
            this._searchQuery = e.target.value;
            this._renderGraph();
          };
          
          if (this.shadowRoot.activeElement === searchInput || this._searchQuery) {
            searchInput.focus();
            const val = searchInput.value;
            searchInput.value = '';
            searchInput.value = val;
          }
        }
 
        // Add click listener on switch header
        groupedEl.querySelectorAll('.switch-group').forEach(group => {
          const header = group.querySelector('.switch-header');
          if (header) {
            header.onclick = (e) => {
              e.stopPropagation();
              const id = group.getAttribute('data-id');
              const node = this._nodes[id];
              if (node) {
                const name = (node.attrs?.name || '').toLowerCase();
                const isAP = node.attrs?.is_ap || name.startsWith('ap ') || name.startsWith('ap-') || name === 'ap' || id.includes(':ap:');
                if (node.attrs?.integration === 'snmp_switch_manager' && !isAP) {
                  this._openSwitchCard(node);
                } else {
                  this._openDeviceDetailsCard(node);
                }
              }
            };
          }
        });
 
        // Add click listener to open device info or switch card on port cards
        groupedEl.querySelectorAll('.port-card').forEach(card => {
          card.onclick = () => {
            try {
              const id = card.getAttribute('data-id');
              const node = this._nodes[id];
              if (node) {
                const name = (node.attrs?.name || '').toLowerCase();
                const isAP = node.attrs?.is_ap || name.startsWith('ap ') || name.startsWith('ap-') || name === 'ap' || id.includes(':ap:');
                if (node.attrs?.integration === 'snmp_switch_manager' && !isAP) {
                  this._openSwitchCard(node, id);
                } else {
                  this._openDeviceDetailsCard(node, id);
                }
              }
            } catch (err) {
              alert("Grouped Port Click Error: " + err.message + "\n" + err.stack);
              console.error(err);
            }
          };
        });
      }
      return;
    }

    const edgesG = this.shadowRoot.getElementById('edges'); const nodesG = this.shadowRoot.getElementById('nodes');
    if (!edgesG || !nodesG) return;

    edgesG.innerHTML = this._edges.map(edge => {
      const src = this._nodes[edge.src]; const dst = this._nodes[edge.dst];
      if (!src || !dst) return '';
      
      const portInfo = edge.attrs?.bridge_port ? `Port ${edge.attrs.bridge_port}` : 'Link';
      const srcName = src.attrs?.name || edge.src.split(':').pop();
      const dstName = dst.attrs?.name || edge.dst.split(':').pop();
      
      const speed = edge.attrs?.speed;
      let speedStr = '';
      let speedClass = '';
      if (speed !== undefined) {
        const speedVal = parseInt(speed, 10);
        speedStr = speedVal >= 1000 ? ` - ${speedVal / 1000} Gbps` : ` - ${speedVal} Mbps`;
        if (speedVal >= 10000) {
          speedClass = ' speed-10g';
        } else if (speedVal >= 1000) {
          speedClass = ' speed-1g';
        } else if (speedVal > 0 && speedVal <= 100) {
          speedClass = ' speed-100m';
        }
      }
      
      const title = `${srcName} -> ${dstName} (${portInfo}${speedStr})`;
      const edgeId = `edge-${edge.src.replace(/:/g, '_')}-${edge.dst.replace(/:/g, '_')}`;
      const hoverId = `edge-hover-${edge.src.replace(/:/g, '_')}-${edge.dst.replace(/:/g, '_')}`;
      
      return `
        <line class="edge-hover" id="${hoverId}" x1="${src.x}" y1="${src.y}" x2="${dst.x}" y2="${dst.y}" stroke="rgba(0,0,0,0.01)" stroke-width="15" style="cursor: pointer; pointer-events: stroke;"><title>${title}</title></line>
        <line class="edge ${edge.kind}${speedClass}" id="${edgeId}" x1="${src.x}" y1="${src.y}" x2="${dst.x}" y2="${dst.y}"><title>${title}</title></line>
      `;
    }).join('');

    const customIcons = this._config.custom_icons || {};

    nodesG.innerHTML = Object.entries(this._nodes).map(([id, node]) => {
      const radius = node.kind === 'device' ? 14 : 6;
      const name = node.attrs?.name || id.split(':').pop();
      const ip = node.attrs?.ip ? ` (${node.attrs.ip})` : '';
      const entity = node.attrs?.entity_id ? this._hass.states[node.attrs.entity_id] : null;
      const currentState = node.attrs?.state || 'up';
      const isDown = currentState === 'down';
      const isAsleep = currentState === 'asleep';
      const isAway = currentState === 'away';
      
      const stateClass = isDown ? 'down' : (isAsleep ? 'asleep' : (isAway ? 'away' : ''));
      const stateSuffix = isDown ? ' (DOWN)' : (isAsleep ? ' (ASLEEP)' : (isAway ? ' (AWAY)' : ''));
      
      const friendlyName = entity?.attributes?.friendly_name;
      const isMac = /^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$/.test(name);
      const isIp = /^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(name);
      let displayName = name;
      if (node.kind !== 'device' && (!displayName || isMac || isIp || displayName === '10' || displayName === '192')) {
        if (friendlyName && !/^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$/.test(friendlyName) && friendlyName !== node.attrs?.entity_id) {
          displayName = friendlyName;
        }
      }
      if (displayName && displayName.endsWith(' Restart')) {
        displayName = displayName.substring(0, displayName.length - 8);
      }
      
      const hoverText = `${displayName}${ip}${stateSuffix}`;
      
      let icon = 'mdi:desktop-tower'; let specificKind = node.kind;
      let clientCountStr = '';
      
      if (node.kind === 'device') {
        const isGateway = node.attrs?.is_gateway || name.toLowerCase().includes('gateway') || name.toLowerCase().includes('router') || name.toLowerCase().includes('firewall');
        if (isGateway) { icon = 'mdi:shield-key'; specificKind = 'gateway'; }
        else if ((node.attrs && node.attrs.is_ap) || name.startsWith('AP') || name.toLowerCase().startsWith('ap ') || name.toLowerCase().startsWith('ap-') || name.toLowerCase() === 'ap') {
          icon = 'mdi:access-point';
          specificKind = 'ap';
          const clientCount = this._edges.filter(e => e.dst === id && this._nodes[e.src]?.kind === 'endpoint').length;
          clientCountStr = ` (${clientCount})`;
        }
        else {
          icon = 'mdi:router-network';
          specificKind = 'switch';
          const wiredCount = this._edges.filter(e => e.dst === id && this._nodes[e.src]?.kind === 'endpoint').length;
          if (wiredCount > 0) clientCountStr = ` (${wiredCount})`;
        }
      } else if (node.kind === 'endpoint') icon = 'mdi:lan-connect';

      if (customIcons[name]) icon = customIcons[name];
      else if (customIcons[node.kind]) icon = customIcons[node.kind];
      else if (customIcons[specificKind]) icon = customIcons[specificKind];

      const showEndpointLabels = this._config.show_endpoint_labels || 'auto';
      let showText = false;
      if (node.kind === 'device') {
        showText = true;
      } else if (node.kind === 'endpoint') {
        if (showEndpointLabels === 'always') {
          showText = true;
        } else if (showEndpointLabels === 'never') {
          showText = false;
        } else {
          const endpointNodesCount = Object.values(this._nodes).filter(n => n.kind === 'endpoint').length;
          showText = endpointNodesCount <= 40;
        }
      }
      const isImage = icon.startsWith('http') || icon.startsWith('/local');
      const unlinkedClass = (showUnlinked && !node.attrs?.device_id) ? 'unlinked' : '';

      const nodeKey = `node-${id.replace(/:/g, '_')}`;
      if (node.kind === 'device') {
        return `
          <g class="node ${specificKind} ${stateClass} ${unlinkedClass}" id="${nodeKey}" transform="translate(${node.x},${node.y})" data-id="${id}">
            <title>${hoverText}${clientCountStr}</title>
            <circle r="${radius}"></circle>
            <foreignObject x="-${radius * 0.8}" y="-${radius * 0.8}" width="${radius * 1.6}" height="${radius * 1.6}">
              ${isImage ? 
                `<img src="${icon}" style="width: 100%; height: 100%; border-radius: 50%;" />` :
                `<ha-icon icon="${icon}" style="--mdc-icon-size: ${radius * 1.6}px; display: flex; align-items: center; justify-content: center;"></ha-icon>`
              }
            </foreignObject>
            ${showText ? `<text dx="${radius + 5}" dy="4">${displayName}${clientCountStr}</text>` : ''}
          </g>
        `;
      } else {
        return `
          <g class="node endpoint ${stateClass} ${unlinkedClass}" id="${nodeKey}" transform="translate(${node.x},${node.y})" data-id="${id}">
            <title>${hoverText}</title>
            <circle r="${radius}"></circle>
            ${showText ? `<text dx="${radius + 5}" dy="4">${displayName}</text>` : ''}
          </g>
        `;
      }
    }).join('');
    
    // Populate DOM element cache
    this._domCache = { nodes: {}, edges: {} };
    
    Object.keys(this._nodes).forEach(id => {
      const nodeKey = `node-${id.replace(/:/g, '_')}`;
      const nodeEl = this.shadowRoot.getElementById(nodeKey);
      if (nodeEl) {
        this._domCache.nodes[id] = nodeEl;
      }
    });
    
    this._edges.forEach(edge => {
      if (!edge.src || !edge.dst) return;
      const edgeKey = `edge-${edge.src.replace(/:/g, '_')}-${edge.dst.replace(/:/g, '_')}`;
      const hoverKey = `edge-hover-${edge.src.replace(/:/g, '_')}-${edge.dst.replace(/:/g, '_')}`;
      
      const lineEl = this.shadowRoot.getElementById(edgeKey);
      const hoverLineEl = this.shadowRoot.getElementById(hoverKey);
      
      if (lineEl) this._domCache.edges[edgeKey] = lineEl;
      if (hoverLineEl) this._domCache.edges[hoverKey] = hoverLineEl;
    });
  }

  getCardSize() { return 12; }
}

// --- GUI Editor Class ---
class SnmpNetworkManagerCardEditor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = null;
    this._hass = null;
    
    this.shadowRoot.innerHTML = `
      <style>
        .option { margin-bottom: 24px; }
        label { display: block; font-weight: bold; margin-bottom: 8px; color: var(--primary-text-color); }
        input, select { width: 100%; padding: 10px; border-radius: 4px; border: 1px solid var(--divider-color); background: var(--card-background-color); color: var(--primary-text-color); box-sizing: border-box; }
        .help { font-size: 0.85rem; color: var(--secondary-text-color); margin-top: 6px; }
        .layout-list { margin-top: 12px; }
        .layout-item { display: flex; justify-content: space-between; align-items: center; padding: 6px 0; border-bottom: 1px solid var(--divider-color); }
        .layout-item button { background: var(--error-color); color: white; border: none; padding: 4px 8px; border-radius: 4px; cursor: pointer; }
        .row { display: flex; gap: 12px; flex-wrap: wrap; }
        .row div { flex: 1; min-width: 120px; }
        .toggle-row { display: flex; align-items: center; justify-content: space-between; }
        .toggle-row input { width: auto; }
        .color-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 16px; margin-top: 12px; }
        .color-picker { display: flex; align-items: center; gap: 10px; }
        .color-picker input[type="color"] { width: 40px; height: 40px; padding: 0; border: none; border-radius: 4px; cursor: pointer; }
        .btn-reset { background: var(--secondary-background-color); color: var(--primary-text-color); border: 1px solid var(--divider-color); padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 0.85rem; }
      </style>
      
      <details open style="margin-bottom: 16px; border: 1px solid var(--divider-color); border-radius: 6px; padding: 12px; background: rgba(255,255,255,0.01);">
        <summary style="font-weight: bold; font-size: 1.05rem; cursor: pointer; user-select: none; margin-bottom: 8px; color: var(--primary-color);">General Settings</summary>
        
        <div class="option">
          <label>Default Layout Mode</label>
          <select id="layout_mode"></select>
          <div class="help">Select the default layout to use when the card loads.</div>
        </div>

        <div class="option toggle-row" style="margin-top: 12px;">
          <label>Fill Available Space</label>
          <input type="checkbox" id="fill">
        </div>

        <div class="option toggle-row" style="margin-top: 12px;">
          <label>Show Unlinked Indicator</label>
          <input type="checkbox" id="show_unlinked_indicator">
        </div>

        <div class="option" id="dimensions-row" style="margin-top: 12px;">
          <label>Dimensions</label>
          <div class="row">
            <div>
              <label style="font-size: 0.85rem;">Width</label>
              <input type="number" id="card_width">
            </div>
            <div>
              <label style="font-size: 0.85rem;">Height</label>
              <input type="number" id="card_height">
            </div>
          </div>
        </div>
      </details>

      <details open style="margin-bottom: 16px; border: 1px solid var(--divider-color); border-radius: 6px; padding: 12px; background: rgba(255,255,255,0.01);">
        <summary style="font-weight: bold; font-size: 1.05rem; cursor: pointer; user-select: none; margin-bottom: 8px; color: var(--primary-color);">Label Settings</summary>
        
        <div class="option">
          <label>Show Endpoint Labels</label>
          <select id="show_endpoint_labels">
            <option value="auto">Auto (Hide if > 40 endpoints)</option>
            <option value="always">Always Show</option>
            <option value="never">Never Show</option>
          </select>
          <div class="help">Configure when client/endpoint labels are displayed on the map.</div>
        </div>
      </details>

      <details open style="margin-bottom: 16px; border: 1px solid var(--divider-color); border-radius: 6px; padding: 12px; background: rgba(255,255,255,0.01);">
        <summary style="font-weight: bold; font-size: 1.05rem; cursor: pointer; user-select: none; margin-bottom: 8px; color: var(--primary-color);">State & Theme Colors</summary>
        
        <div class="option">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
            <span style="font-size: 0.85rem; color: var(--secondary-text-color);">Set custom node and status colors.</span>
            <button id="btn-reset-colors" class="btn-reset">Reset to Default</button>
          </div>
          
          <div style="font-weight: bold; font-size: 0.8rem; color: var(--secondary-text-color); margin-bottom: 6px; border-bottom: 1px solid var(--divider-color); padding-bottom: 2px;">Network Infrastructure</div>
          <div class="color-grid" style="margin-bottom: 16px;">
            <div class="color-picker">
              <input type="color" id="gateway_color">
              <label>Gateway</label>
            </div>
            <div class="color-picker">
              <input type="color" id="switch_color">
              <label>Switch</label>
            </div>
            <div class="color-picker">
              <input type="color" id="ap_color">
              <label>AP</label>
            </div>
          </div>
          
          <div style="font-weight: bold; font-size: 0.8rem; color: var(--secondary-text-color); margin-bottom: 6px; border-bottom: 1px solid var(--divider-color); padding-bottom: 2px;">Client Nodes</div>
          <div class="color-grid" style="margin-bottom: 16px;">
            <div class="color-picker">
              <input type="color" id="endpoint_color">
              <label>Endpoint</label>
            </div>
          </div>
          
          <div style="font-weight: bold; font-size: 0.8rem; color: var(--secondary-text-color); margin-bottom: 6px; border-bottom: 1px solid var(--divider-color); padding-bottom: 2px;">Connection States</div>
          <div class="color-grid">
            <div class="color-picker">
              <input type="color" id="asleep_color">
              <label>Asleep State</label>
            </div>
            <div class="color-picker">
              <input type="color" id="away_color">
              <label>Away State</label>
            </div>
            <div class="color-picker">
              <input type="color" id="down_color">
              <label>Down State</label>
            </div>
          </div>
        </div>
      </details>

      <details open style="margin-bottom: 16px; border: 1px solid var(--divider-color); border-radius: 6px; padding: 12px; background: rgba(255,255,255,0.01);">
        <summary style="font-weight: bold; font-size: 1.05rem; cursor: pointer; user-select: none; margin-bottom: 8px; color: var(--primary-color);">Custom Device Icons</summary>
        
        <div class="option">
          <div class="row" style="margin-top: 12px;">
            <select id="icon_key_select" style="flex: 1;"></select>
            <input type="text" id="icon_key_input" placeholder="Enter Device Name" style="display: none; flex: 1;">
            <input type="text" id="icon_value" list="icon-datalist" placeholder="mdi:icon or /local/image.png" style="flex: 1;">
            <button id="btn-add-icon" style="padding: 10px;">Add</button>
          </div>
          <div id="icon-list" style="margin-top: 12px;"></div>
        </div>
      </details>
      
      <datalist id="icon-datalist">
        <option value="mdi:desktop-tower">
        <option value="mdi:access-point">
        <option value="mdi:router-network">
        <option value="mdi:lan-connect">
        <option value="mdi:shield-key">
        <option value="mdi:cellphone">
        <option value="mdi:tablet">
        <option value="mdi:printer">
        <option value="mdi:television">
        <option value="mdi:camera">
        <option value="mdi:server">
        <option value="mdi:nas">
        <option value="mdi:motion-sensor">
        <option value="mdi:lightbulb">
        <option value="mdi:power-plug">
      </datalist>
    `;
    
    this._setupEvents();
  }

  setConfig(config) {
    this._config = config;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
  }

  _setupEvents() {
    const root = this.shadowRoot;
    
    root.getElementById('layout_mode').addEventListener('change', (e) => this._valueChanged('layout_mode', e.target.value));
    
    const fillCheckbox = root.getElementById('fill');
    const dimensionsRow = root.getElementById('dimensions-row');
    
    fillCheckbox.addEventListener('change', (e) => {
      this._valueChanged('fill', e.target.checked);
      dimensionsRow.style.display = e.target.checked ? 'none' : 'block';
    });
    
    root.getElementById('card_width').addEventListener('change', (e) => this._valueChanged('width', parseInt(e.target.value)));
    root.getElementById('card_height').addEventListener('change', (e) => this._valueChanged('height', parseInt(e.target.value)));
    
    root.getElementById('gateway_color').addEventListener('change', (e) => this._valueChanged('gateway_color', e.target.value));
    root.getElementById('switch_color').addEventListener('change', (e) => this._valueChanged('switch_color', e.target.value));
    root.getElementById('ap_color').addEventListener('change', (e) => this._valueChanged('ap_color', e.target.value));
    root.getElementById('endpoint_color').addEventListener('change', (e) => this._valueChanged('endpoint_color', e.target.value));
    root.getElementById('down_color').addEventListener('change', (e) => this._valueChanged('down_color', e.target.value));
    root.getElementById('asleep_color').addEventListener('change', (e) => this._valueChanged('asleep_color', e.target.value));
    root.getElementById('away_color').addEventListener('change', (e) => this._valueChanged('away_color', e.target.value));
    root.getElementById('show_endpoint_labels').addEventListener('change', (e) => this._valueChanged('show_endpoint_labels', e.target.value));
    root.getElementById('show_unlinked_indicator').addEventListener('change', (e) => this._valueChanged('show_unlinked_indicator', e.target.checked));

    root.getElementById('btn-reset-colors').addEventListener('click', () => {
      const newConfig = { ...this._config };
      delete newConfig.gateway_color;
      delete newConfig.switch_color;
      delete newConfig.ap_color;
      delete newConfig.endpoint_color;
      delete newConfig.down_color;
      delete newConfig.asleep_color;
      delete newConfig.away_color;
      this.dispatchEvent(new CustomEvent('config-changed', { detail: { config: newConfig }, bubbles: true, composed: true }));
    });
    
    const keySelect = root.getElementById('icon_key_select');
    const keyInput = root.getElementById('icon_key_input');
    
    keySelect.addEventListener('change', (e) => {
      keyInput.style.display = e.target.value === 'custom' ? 'block' : 'none';
    });

    root.getElementById('btn-add-icon').addEventListener('click', () => {
      let key = keySelect.value;
      if (key === 'custom') key = keyInput.value;
      const val = root.getElementById('icon_value').value;
      if (key && val) {
        const customIcons = { ...this._config.custom_icons || {} };
        customIcons[key] = val;
        this._valueChanged('custom_icons', customIcons);
      }
    });
  }

  _render() {
    if (!this._config) return;

    const root = this.shadowRoot;
    const fill = this._config.fill !== false;

    const layoutSelect = root.getElementById('layout_mode');
    layoutSelect.innerHTML = `
      <option value="clustered" ${this._config.layout_mode === 'clustered' ? 'selected' : ''}>Clustered</option>
      <option value="concentric" ${this._config.layout_mode === 'concentric' ? 'selected' : ''}>Concentric</option>
      <option value="force" ${this._config.layout_mode === 'force' ? 'selected' : ''}>Force</option>
      <option value="grid" ${this._config.layout_mode === 'grid' ? 'selected' : ''}>Grid</option>
      ${Object.keys(this._config.layouts || {}).map(m => `
        <option value="${m}" ${this._config.layout_mode === m ? 'selected' : ''}>${m}</option>
      `).join('')}
    `;

    root.getElementById('fill').checked = fill;
    root.getElementById('dimensions-row').style.display = fill ? 'none' : 'block';
    root.getElementById('card_width').value = this._config.width || 1000;
    root.getElementById('card_height').value = this._config.height || 600;

    root.getElementById('gateway_color').value = this._config.gateway_color || '#d500f9';
    root.getElementById('switch_color').value = this._config.switch_color || '#00e5ff';
    root.getElementById('ap_color').value = this._config.ap_color || '#00e5ff';
    root.getElementById('endpoint_color').value = this._config.endpoint_color || '#00e676';
    root.getElementById('down_color').value = this._config.down_color || '#ff0000';
    root.getElementById('asleep_color').value = this._config.asleep_color || '#ffb300';
    root.getElementById('away_color').value = this._config.away_color || '#b388ff';
    root.getElementById('show_endpoint_labels').value = this._config.show_endpoint_labels || 'auto';
    root.getElementById('show_unlinked_indicator').checked = this._config.show_unlinked_indicator !== false;

    const keySelect = root.getElementById('icon_key_select');
    keySelect.innerHTML = `
      <option value="">-- Select Type --</option>
      <option value="gateway">gateway</option>
      <option value="switch">switch</option>
      <option value="ap">ap</option>
      <option value="endpoint">endpoint</option>
      <option value="custom">-- Custom Name --</option>
    `;

    const iconList = root.getElementById('icon-list');
    iconList.innerHTML = Object.entries(this._config.custom_icons || {}).map(([key, val]) => `
      <div class="layout-item">
        <span><b>${key}</b>: ${val}</span>
        <button data-key="${key}" class="btn-delete-icon">Delete</button>
      </div>
    `).join('') || 'No custom icons set.';

    root.querySelectorAll('.btn-delete-icon').forEach(btn => {
      btn.addEventListener('click', (e) => {
        const key = e.target.dataset.key;
        const customIcons = { ...this._config.custom_icons };
        delete customIcons[key];
        this._valueChanged('custom_icons', customIcons);
      });
    });
  }

  _valueChanged(key, value) {
    const newConfig = { ...this._config };
    newConfig[key] = value;
    this.dispatchEvent(new CustomEvent('config-changed', { detail: { config: newConfig }, bubbles: true, composed: true }));
  }
}

if (!customElements.get("snmp-network-manager-card-editor")) {
  customElements.define("snmp-network-manager-card-editor", SnmpNetworkManagerCardEditor);
}

if (!customElements.get("snmp-network-manager-card")) { customElements.define("snmp-network-manager-card", SnmpNetworkManagerCard); }

window.customCards = window.customCards || [];
const existingCard = window.customCards.find(c => c.type === "snmp-network-manager-card");
if (!existingCard) {
  window.customCards.push({
    type: "snmp-network-manager-card",
    name: "SNMP Network Manager Card",
    description: "A card to visualize network topology.",
    preview: true,
  });
}
