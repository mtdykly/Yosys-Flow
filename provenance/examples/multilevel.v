module demo(input a, b, c, d, e, f, output y, z);
wire t, u;
assign t = a & b;
assign u = t ^ c;
assign y = u ^ d;
assign z = e | f;
endmodule
