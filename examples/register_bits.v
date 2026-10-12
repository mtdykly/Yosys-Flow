module demo(input clk, r0, r1, e0, e1, a, b, output reg [1:0] q);
initial q = 2'b10;
always @(posedge clk) begin
  if (r0) q[0] <= 0; else if (e0) q[0] <= a;
  if (r1) q[1] <= 1; else if (e1) q[1] <= b;
end
endmodule
