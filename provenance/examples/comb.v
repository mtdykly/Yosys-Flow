module demo(input en, a, b, output reg y, output direct, zero);
  always @* begin
    if (en)
      y = a & b;
    else
      y = a | b;
  end
  assign direct = a;
  assign zero = 1'b0;
endmodule
