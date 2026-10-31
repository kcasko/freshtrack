// FreshTrack settings.
//
// apiKey is an Amazon Location Service API key for the map tiles, address
// search, and routes. Keys like this are meant to live in browser code: it is
// restricted (deploy/deploy_site.py) to only map tiles, geocoding, and route
// calculation, and only from FreshTrack's own sites. Leave it empty to run with
// a free demo basemap, straight-line distances, and address search through the
// chat agent instead.
window.KZOO_FOOD_MAP_CONFIG = {
  region: "us-east-1",
  apiKey: "",
  mapStyle: "Standard",
  // FreshTrack agent chat (lambda/chat.py). When the page is served by
  // deploy/local_server.py the chat goes to its local /chat route instead.
  chatEndpoint: ["localhost", "127.0.0.1"].includes(location.hostname)
    ? "/chat"
    : "https://lawtxajsl24jibgv2ljc6cq6ue0lgyqz.lambda-url.us-east-1.on.aws/",
  // Amazon Location Service Maps (v2) style descriptor for MapLibre GL JS.
  amazonStyleUrl: function () {
    return "https://maps.geo." + this.region + ".amazonaws.com/v2/styles/" + this.mapStyle +
      "/descriptor?key=" + this.apiKey + "&color-scheme=Light";
  }
};
