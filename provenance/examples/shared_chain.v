module demo(input a, b, c, d, output y, z);
wire t;
assign t = a & b;
assign y = t ^ c;
assign z = t ^ d;
endmodule
