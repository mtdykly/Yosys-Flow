module demo(input clk, rst, en, a, b, output reg q, output reg y, output direct, zero);
  initial q = 1'b1;
  always @* begin
    if (en)
      y = a & b;
    else
      y = a | b;
  end
  always @(posedge clk or posedge rst) begin
    if (rst)
      q <= 1'b0;
    else if (en)
      q <= y;
  end
  assign direct = a;
  assign zero = 1'b0;
endmodule
