module demo(input a, b, c, d, e, output y, z, independent);
assign y = (a & b) | (a & c);
assign z = a & (b | c);
assign independent = d ^ e;
endmodule
