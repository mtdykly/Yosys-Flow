// Explicit new local specification; its ports bind to the old contract.
// This is NOT sliced from a fully synthesized new design.
module patch(input [7:0] a, b, c, output [7:0] y);
  assign y = (a | b) ^ c;
endmodule
