// A software Matter On/Off light for the VM acceptance run — the same approach
// matterjs-server's own CI uses. It speaks the real Matter protocol over IP and
// is commissioned with the well-known test code 34970112332 (passcode
// 20202021, discriminator 3840). CI only: this is not a pilot device.
import { Environment, ServerNode } from "@matter/main";
import { OnOffLightDevice } from "@matter/main/devices/on-off-light";
import { VendorId } from "@matter/main/types";

Environment.default.vars.set("storage.path", "/tmp/virtual-light");

const node = await ServerNode.create({
  id: "virtual-light",
  network: { port: 5550 },
  commissioning: { passcode: 20202021, discriminator: 3840 },
  productDescription: { name: "Virtual Light", deviceType: OnOffLightDevice.deviceType },
  basicInformation: {
    vendorName: "Test Vendor", vendorId: VendorId(0xfff1), productName: "Virtual Light",
    productId: 0x8000, nodeLabel: "Virtual Light", serialNumber: "VL-0001", uniqueId: "virtual-light-0001",
  },
});
const light = await node.add(OnOffLightDevice, { id: "light" });
light.events.onOff.onOff$Changed.on(value => console.log(`VIRTUAL-LIGHT onOff=${value}`));
console.log("VIRTUAL-LIGHT ready for commissioning");
await node.run();
