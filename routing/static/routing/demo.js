(() => {
    "use strict";

    const root = document.querySelector("[data-route-demo]");

    if (!root) {
        return;
    }

    const elements = {
        form: root.querySelector("[data-route-form]"),
        start: root.querySelector('[name="start"]'),
        finish: root.querySelector('[name="finish"]'),
        swap: root.querySelector("[data-swap-locations]"),
        submit: root.querySelector('[type="submit"]'),
        buttonLabel: root.querySelector("[data-button-label]"),
        buttonSpinner: root.querySelector("[data-button-spinner]"),
        status: root.querySelector("[data-status-message]"),
        summary: root.querySelector("[data-result-summary]"),
        routeTitle: root.querySelector("[data-route-title]"),
        distance: root.querySelector("[data-distance]"),
        duration: root.querySelector("[data-duration]"),
        stopCount: root.querySelector("[data-stop-count]"),
        totalCost: root.querySelector("[data-total-cost]"),
        stopList: root.querySelector("[data-stop-list]"),
        map: root.querySelector("[data-route-map]"),
        mapMessage: root.querySelector("[data-map-message]"),
    };

    const apiUrl = root.dataset.apiUrl;
    const mapApiKey = root.dataset.mapApiKey;
    let map;
    let resultLayer;
    let stationLayers = new Map();

    function initializeMap() {
        if (typeof window.L === "undefined") {
            showMapError(
                "The map library could not be loaded. Check your network connection."
            );
            elements.submit.disabled = true;
            return;
        }

        if (!mapApiKey) {
            showMapError(
                "GEOAPIFY_API_KEY is not configured for map tiles."
            );
            elements.submit.disabled = true;
            return;
        }

        map = window.L.map(elements.map, {
            preferCanvas: true,
            minZoom: 3,
        }).setView([39.5, -98.35], 4);

        const resolutionSuffix = window.L.Browser.retina ? "@2x" : "";
        const tileUrl =
            "https://maps.geoapify.com/v1/tile/positron/" +
            `{z}/{x}/{y}${resolutionSuffix}.png?apiKey=` +
            encodeURIComponent(mapApiKey);

        window.L.tileLayer(
            tileUrl,
            {
                maxZoom: 20,
                attribution:
                    'Powered by <a href="https://www.geoapify.com/">Geoapify</a> | ' +
                    '<a href="https://openmaptiles.org/">&copy; OpenMapTiles</a> ' +
                    '<a href="https://www.openstreetmap.org/copyright">&copy; OpenStreetMap</a> contributors',
            }
        ).addTo(map);
    }

    function setLoading(isLoading) {
        elements.submit.disabled = isLoading;
        elements.start.disabled = isLoading;
        elements.finish.disabled = isLoading;
        elements.swap.disabled = isLoading;
        elements.buttonLabel.textContent = isLoading
            ? "Calculating route"
            : "Calculate route";
        elements.buttonSpinner.hidden = !isLoading;
    }

    function setStatus(message, type = "neutral") {
        elements.status.textContent = message;
        elements.status.classList.toggle("is-error", type === "error");
        elements.status.classList.toggle("is-success", type === "success");
    }

    function showMapError(message) {
        elements.mapMessage.hidden = false;
        elements.mapMessage.classList.add("is-error");
        elements.mapMessage.querySelector("strong").textContent =
            "Map unavailable";
        elements.mapMessage.querySelector("span").textContent = message;
    }

    function clearResult() {
        elements.summary.hidden = true;
        elements.stopList.replaceChildren();
        stationLayers = new Map();

        if (map && resultLayer) {
            map.removeLayer(resultLayer);
            resultLayer = null;
        }
    }

    function formatNumber(value, maximumFractionDigits = 2) {
        return new Intl.NumberFormat("en-US", {
            maximumFractionDigits,
        }).format(value);
    }

    function formatDuration(minutes) {
        const totalMinutes = Math.round(minutes);
        const hours = Math.floor(totalMinutes / 60);
        const remainingMinutes = totalMinutes % 60;

        if (!hours) {
            return `${remainingMinutes} min`;
        }

        return `${hours} hr ${remainingMinutes} min`;
    }

    function createTextElement(tagName, className, text) {
        const element = document.createElement(tagName);

        if (className) {
            element.className = className;
        }

        element.textContent = text;
        return element;
    }

    function createPopup(feature, stop) {
        const properties = feature.properties;
        const popup = document.createElement("div");
        popup.className = "map-popup";

        if (properties.feature_type === "fuel_stop" && stop) {
            popup.append(
                createTextElement(
                    "p",
                    "popup-label",
                    `Fuel stop ${stop.sequence}`
                ),
                createTextElement("h3", "", stop.name),
                createTextElement(
                    "p",
                    "",
                    `${stop.address}, ${stop.city}, ${stop.state}`
                )
            );

            const metrics = document.createElement("div");
            metrics.className = "popup-metrics";

            [
                ["Fuel price", `$${stop.price_per_gallon}/gal`],
                ["Purchase", `${formatNumber(stop.gallons_purchased, 3)} gal`],
                ["Stop cost", `$${stop.stop_cost}`],
                ["Route mile", formatNumber(stop.distance_along_route_miles)],
            ].forEach(([label, value]) => {
                const metric = document.createElement("div");
                metric.append(
                    createTextElement("span", "", label),
                    createTextElement("strong", "", value)
                );
                metrics.append(metric);
            });

            popup.append(metrics);
            return popup;
        }

        const label = properties.label || "Route point";
        const type = properties.feature_type === "start"
            ? "Starting location"
            : "Destination";

        popup.append(
            createTextElement("p", "popup-label", type),
            createTextElement("h3", "", label)
        );
        return popup;
    }

    function pointStyle(feature) {
        const type = feature.properties.feature_type;
        const colors = {
            start: "#23845b",
            fuel_stop: "#e17b2d",
            finish: "#c64b4b",
        };

        return {
            radius: type === "fuel_stop" ? 10 : 9,
            color: "#ffffff",
            weight: 3,
            fillColor: colors[type] || "#315bd6",
            fillOpacity: 1,
        };
    }

    function renderMap(geojson, stops) {
        if (!map) {
            return;
        }

        const stopsById = new Map(
            stops.map((stop) => [String(stop.opis_id), stop])
        );

        if (resultLayer) {
            map.removeLayer(resultLayer);
        }

        stationLayers = new Map();
        resultLayer = window.L.geoJSON(geojson, {
            style(feature) {
                if (feature.properties.feature_type !== "route") {
                    return undefined;
                }

                return {
                    color: "#315bd6",
                    weight: 5,
                    opacity: 0.88,
                    lineCap: "round",
                    lineJoin: "round",
                };
            },
            pointToLayer(feature, latlng) {
                return window.L.circleMarker(
                    latlng,
                    pointStyle(feature)
                );
            },
            onEachFeature(feature, layer) {
                const properties = feature.properties;

                if (properties.feature_type === "route") {
                    return;
                }

                const stop = properties.opis_id
                    ? stopsById.get(String(properties.opis_id))
                    : null;

                layer.bindPopup(createPopup(feature, stop));

                if (properties.feature_type === "fuel_stop" && stop) {
                    layer.bindTooltip(String(stop.sequence), {
                        permanent: true,
                        direction: "center",
                        className: "fuel-marker-label",
                    });
                    stationLayers.set(String(stop.opis_id), layer);
                }
            },
        }).addTo(map);

        const bounds = resultLayer.getBounds();

        if (bounds.isValid()) {
            map.fitBounds(bounds, {
                padding: [34, 34],
                maxZoom: 13,
            });
        }

        elements.mapMessage.hidden = true;
        window.requestAnimationFrame(() => map.invalidateSize());
    }

    function createStopMetric(label, value) {
        const metric = document.createElement("div");
        metric.append(
            createTextElement("span", "", label),
            createTextElement("strong", "", value)
        );
        return metric;
    }

    function renderStops(stops) {
        elements.stopList.replaceChildren();

        if (!stops.length) {
            const item = createTextElement(
                "li",
                "empty-stops",
                "No fuel stop is required. The destination is within the initial 500-mile range."
            );
            elements.stopList.append(item);
            return;
        }

        stops.forEach((stop) => {
            const item = document.createElement("li");
            const button = document.createElement("button");
            button.type = "button";
            button.className = "stop-card";

            const header = document.createElement("div");
            header.className = "stop-card-header";
            const details = document.createElement("div");
            details.append(
                createTextElement("h3", "", stop.name),
                createTextElement(
                    "p",
                    "stop-address",
                    `${stop.address}, ${stop.city}, ${stop.state}`
                )
            );
            header.append(
                createTextElement(
                    "span",
                    "stop-number",
                    String(stop.sequence)
                ),
                details
            );

            const metrics = document.createElement("div");
            metrics.className = "stop-metrics";
            metrics.append(
                createStopMetric("Route mile", formatNumber(stop.distance_along_route_miles)),
                createStopMetric("Gallons", formatNumber(stop.gallons_purchased, 3)),
                createStopMetric("Cost", `$${stop.stop_cost}`)
            );

            button.append(header, metrics);
            button.addEventListener("click", () => {
                const layer = stationLayers.get(String(stop.opis_id));

                if (!layer || !map) {
                    return;
                }

                map.flyTo(layer.getLatLng(), Math.max(map.getZoom(), 9), {
                    duration: 0.7,
                });
                layer.openPopup();
            });

            item.append(button);
            elements.stopList.append(item);
        });
    }

    function renderResult(payload) {
        const plan = payload.fuel_plan;

        renderMap(payload.map, plan.stops);
        renderStops(plan.stops);

        elements.routeTitle.textContent =
            `${payload.start.label} to ${payload.finish.label}`;
        elements.distance.textContent = formatNumber(
            payload.route.distance_miles
        );
        elements.duration.textContent = formatDuration(
            payload.route.duration_minutes
        );
        elements.stopCount.textContent = String(plan.stops.length);
        elements.totalCost.textContent = plan.total_cost;
        elements.summary.hidden = false;
    }

    async function requestRoute(start, finish) {
        const response = await fetch(apiUrl, {
            method: "POST",
            headers: {
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            body: JSON.stringify({ start, finish }),
        });

        let payload;

        try {
            payload = await response.json();
        } catch {
            throw new Error("The server returned an unreadable response.");
        }

        if (!response.ok) {
            throw new Error(
                payload.error?.message || "The route could not be calculated."
            );
        }

        if (payload.map?.type !== "FeatureCollection") {
            throw new Error("The server returned invalid map data.");
        }

        return payload;
    }

    elements.swap.addEventListener("click", () => {
        const start = elements.start.value;
        elements.start.value = elements.finish.value;
        elements.finish.value = start;
        elements.start.focus();
    });

    elements.form.addEventListener("submit", async (event) => {
        event.preventDefault();

        const start = elements.start.value.trim();
        const finish = elements.finish.value.trim();

        if (!start || !finish) {
            setStatus("Enter both a start and destination.", "error");
            return;
        }

        clearResult();
        setLoading(true);
        setStatus("Calculating the route and fuel plan...");

        try {
            const payload = await requestRoute(start, finish);
            renderResult(payload);
            setStatus(
                `Route ready with ${payload.fuel_plan.stops.length} recommended fuel stop${payload.fuel_plan.stops.length === 1 ? "" : "s"}.`,
                "success"
            );
        } catch (error) {
            const message = error instanceof Error
                ? error.message
                : "The route could not be calculated.";
            setStatus(message, "error");
            showMapError(message);
        } finally {
            setLoading(false);
        }
    });

    initializeMap();
})();
