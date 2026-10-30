// FreshTrack Amazon Location Service settings.
// Copy this pattern for your own deployment. Do not commit a real API key
// to a public repo. For a public demo, use an Amazon Location API key that
// is restricted to your map, place index, and route calculator resources
// and to your site domain, or use Amazon Cognito for authenticated access.
// Leave apiKey empty to run with the open demo basemap and straight-line
// distance while you test locally.
window.KZOO_FOOD_MAP_CONFIG = {
  region: "us-east-1",
  mapName: "freshtrack-kalamazoo",
  placeIndexName: "freshtrack-place-index",
  routeCalculatorName: "freshtrack-route-calculator",
  apiKey: "",
  // Optional Lambda Function URL for nearest-3 search. Leave empty to use
  // the browser fallback: Place Index search if a key is set, otherwise
  // map clicks and geolocation for the starting point.
  lambdaEndpoint: "",
  // FreshTrack agent chat (lambda/chat.py). When the page is served by
  // deploy/local_server.py the chat goes to its local /chat route instead.
  chatEndpoint: ["localhost", "127.0.0.1"].includes(location.hostname) ? "/chat" : "",
  amazonStyleUrl: function () {
    return "https://maps.geo." + this.region + ".amazonaws.com/maps/v0/maps/" +
      this.mapName + "/style-descriptor?key=" + this.apiKey;
  }
};
